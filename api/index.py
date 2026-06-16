from flask import Flask, request, jsonify
import requests
import json
import os
import anthropic

app = Flask(__name__)

LUSHA_BASE = "https://api.lusha.com"
LUSHA_KEY = os.environ.get("LUSHA_API_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

HTML = open(os.path.join(os.path.dirname(__file__), "index.html")).read() if os.path.exists(os.path.join(os.path.dirname(__file__), "index.html")) else "<h1>Loading...</h1>"

INDUSTRY_TAXONOMY = """
Lusha industry taxonomy:
MAIN INDUSTRIES (mainIndustriesIds):
1=Hospitality, 2=Agriculture, 3=Construction, 4=Manufacturing,
5=Community & Nonprofit, 6=Education, 7=Entertainment, 8=Finance,
9=Government, 10=Healthcare, 11=Legal, 12=Media & Communications,
13=Professional Services, 14=Real Estate, 15=Technology,
16=Retail & Wholesale, 17=Transportation & Logistics, 18=Utilities & Energy

SUB INDUSTRIES (subIndustriesIds):
Hotels & Accommodation Services=3, Restaurants=2, Food & Beverage Services=1,
Events Services=5, Travel & Reservation Services=11, Insurance=44,
Motor Vehicles=87, Motor Vehicle Parts Dealers=146, Sports=32,
Entertainment Providers=28, General Merchandise Retail=115,
Shopping Centers=116, Hospitals & Health Systems=55, Aged Care=56,
Private Hospitals=57, Medical Practices=58, Pharmaceuticals=60,
Banks=38, Financial Planning=41, Accounting=42, Law Firms=64,
Software Development=80, Telecommunications=81, Airlines=90,
Freight & Logistics=91, Mining=95, Oil & Gas=96, Universities=24
"""

def parse_natural_language(query, country):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    prompt = f"""Parse this B2B prospecting request into Lusha API filter parameters.

User request: "{query}"
Country: {country}

{INDUSTRY_TAXONOMY}

Return ONLY valid JSON:
{{
  "jobTitles": ["title1", "title2"],
  "mainIndustriesIds": [],
  "subIndustriesIds": [],
  "explanation": "one sentence summary"
}}

Include both abbreviated and full title versions (e.g. "CMO" AND "Chief Marketing Officer").
Use subIndustriesIds when specific, mainIndustriesIds for broad categories."""

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"): text = text[4:]
    return json.loads(text.strip())

def lusha_search(api_key, job_title, industry_filter, country, page=0):
    company_include = {}
    if industry_filter.get("mainIndustriesIds"):
        company_include["mainIndustriesIds"] = industry_filter["mainIndustriesIds"]
    if industry_filter.get("subIndustriesIds"):
        company_include["subIndustriesIds"] = industry_filter["subIndustriesIds"]
    payload = {
        "pagination": {"page": page, "size": 10},
        "filters": {
            "contacts": {"include": {
                "jobTitles": [job_title],
                "locations": [{"country": country}],
            }},
        }
    }
    if company_include:
        payload["filters"]["companies"] = {"include": company_include}
    r = requests.post(
        f"{LUSHA_BASE}/v3/contacts/prospecting",
        headers={"api_key": api_key, "Content-Type": "application/json"},
        json=payload, timeout=30
    )
    if r.status_code in (200, 201):
        return r.json()
    return None

def lusha_enrich(api_key, contact_id):
    r = requests.post(
        f"{LUSHA_BASE}/v3/contacts/enrich",
        headers={"api_key": api_key, "Content-Type": "application/json"},
        json={"ids": [contact_id], "reveal": ["emails", "phones"]}, timeout=30
    )
    if r.status_code in (200, 201):
        results = r.json().get("results", [])
        return results[0] if results else {}
    return {}

def extract_email(enriched):
    emails = enriched.get("emails", [])
    for e in emails:
        if isinstance(e, dict) and e.get("type") == "work":
            return e.get("email", "")
    if emails:
        e = emails[0]
        return e.get("email", e) if isinstance(e, dict) else str(e)
    return ""

def extract_phone(enriched):
    phones = enriched.get("phones", [])
    for p in phones:
        if isinstance(p, dict): return p.get("phone", "")
    return phones[0] if phones else ""

@app.route("/")
def index():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path) as f:
        return f.read(), 200, {"Content-Type": "text/html"}

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "anthropic_key": bool(ANTHROPIC_KEY), "lusha_key": bool(LUSHA_KEY)})

@app.route("/api/parse", methods=["POST"])
def parse():
    data = request.json
    query = data.get("query", "")
    country = data.get("country", "Australia")
    if not query:
        return jsonify({"error": "No query provided"}), 400
    try:
        parsed = parse_natural_language(query, country)
        return jsonify(parsed)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/search", methods=["POST"])
def search():
    data = request.json
    api_key = request.headers.get("X-Lusha-Key") or LUSHA_KEY
    if not api_key:
        return jsonify({"error": "No Lusha API key provided"}), 401
    job_titles = data.get("jobTitles", [])
    industry_filter = {
        "mainIndustriesIds": data.get("mainIndustriesIds", []),
        "subIndustriesIds": data.get("subIndustriesIds", []),
    }
    country = data.get("country", "Australia")
    max_contacts = min(int(data.get("maxContacts", 50)), 500)
    want_email = data.get("wantEmail", True)
    want_phone = data.get("wantPhone", False)
    if not job_titles:
        return jsonify({"error": "No job titles provided"}), 400
    seen = set()
    results = []
    debug_log = []
    max_per_title = max(10, max_contacts // len(job_titles))
    for title in job_titles:
        count = 0
        for page in range(max(1, max_per_title // 10)):
            if count >= max_per_title: break
            result = lusha_search(api_key, title, industry_filter, country, page)
            debug_log.append({"title": title, "page": page, "status": "ok" if result else "no_result", "count": len(result.get("results", [])) if result else 0})
            if not result: break
            contacts = result.get("results", [])
            if not contacts: break
            for c in contacts:
                if count >= max_per_title: break
                cid = c.get("id") or c.get("contactId") or str(c.get("personId", ""))
                if not cid or cid in seen: continue
                seen.add(cid)
                name = c.get("fullName") or (c.get("firstName", "") + " " + c.get("lastName", "")).strip()
                email = ""
                phone = ""
                if want_email or want_phone:
                    enriched = lusha_enrich(api_key, cid)
                    if want_email: email = extract_email(enriched)
                    if want_phone: phone = extract_phone(enriched)
                results.append({
                    "id": cid, "name": name,
                    "title": c.get("jobTitle", {}).get("title", title) if isinstance(c.get("jobTitle"), dict) else c.get("jobTitle", title),
                    "company": c.get("company", {}).get("name", c.get("companyName", "")),
                    "domain": c.get("company", {}).get("domain", c.get("fqdn", "")),
                    "email": email, "phone": phone,
                    "linkedin": c.get("socialLinks", {}).get("linkedin", c.get("linkedinUrl", "")),
                    "city": c.get("location", {}).get("city", c.get("city", "")),
                    "country": country,
                    "sector": data.get("sectorLabel", "")
                })
                count += 1
        if len(results) >= max_contacts: break
    return jsonify({"total": len(results), "contacts": results[:max_contacts]})

if __name__ == "__main__":
    app.run(debug=True, port=5000)

@app.route("/api/debug_enrich", methods=["POST"])
def debug_enrich():
    data = request.json
    contact_id = data.get("id")
    r = requests.post(
        f"{LUSHA_BASE}/v3/contacts/enrich",
        headers={"api_key": LUSHA_KEY, "Content-Type": "application/json"},
        json={"ids": [contact_id], "reveal": ["emails", "phones"]}, timeout=30
    )
    return jsonify({"status": r.status_code, "body": r.json()})

@app.route("/api/debug_search", methods=["POST"])
def debug_search():
    payload = {
        "pagination": {"page": 0, "size": 10},
        "filters": {
            "contacts": {"include": {
                "jobTitles": ["CFO"],
                "locations": [{"country": "Australia"}],
            }},
            "companies": {"include": {"mainIndustriesIds": [15]}}
        }
    }
    r = requests.post(
        f"{LUSHA_BASE}/v3/contacts/prospecting",
        headers={"api_key": LUSHA_KEY, "Content-Type": "application/json"},
        json=payload, timeout=30
    )
    return jsonify({"status": r.status_code, "body": r.json()})
