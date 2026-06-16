from flask import Flask, request, jsonify
import requests
import json
import os
import anthropic

app = Flask(__name__)

APOLLO_BASE = "https://api.apollo.io/api/v1"
APOLLO_KEY = os.environ.get("APOLLO_API_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def parse_natural_language(query, country):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    prompt = f"""Parse this B2B prospecting request into search filter parameters.

User request: "{query}"
Country: {country}

Return ONLY valid JSON with no explanation or markdown:
{{
  "jobTitles": ["title1", "title2"],
  "industryKeywords": ["keyword1"],
  "sectorLabel": "short label e.g. 'Healthcare' or 'SaaS Technology'",
  "explanation": "one sentence summary"
}}

Rules:
- jobTitles: include both abbreviated and full versions (e.g. "CMO" AND "Chief Marketing Officer")
- industryKeywords: MAX 1 short industry keyword (e.g. "healthcare", "software", "hospitality", "finance"). Use simple common words only. Leave empty [] if no industry specified or multiple unrelated industries requested.
- sectorLabel: a SHORT human-readable label for the sector badge (max 3 words). Empty string if no industry or multiple industries.
- explanation: one sentence describing what will be searched"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def apollo_search(api_key, job_titles, industry_keywords, country, page=1, per_page=10, city=""):
    parts = []
    for title in job_titles:
        parts.append(f"person_titles[]={requests.utils.quote(title)}")
    if city:
        parts.append(f"person_locations[]={requests.utils.quote(city + ', ' + country)}")
    elif country:
        parts.append(f"person_locations[]={requests.utils.quote(country)}")
    if industry_keywords:
        parts.append(f"q_keywords={requests.utils.quote(industry_keywords[0])}")
    parts.append(f"per_page={per_page}")
    parts.append(f"page={page}")

    url = f"{APOLLO_BASE}/mixed_people/api_search?{'&'.join(parts)}"
    r = requests.post(
        url,
        headers={"x-api-key": api_key, "Content-Type": "application/json", "Cache-Control": "no-cache"},
        timeout=30
    )
    if r.status_code == 200:
        return r.json()
    return None


def apollo_bulk_enrich(api_key, person_ids, want_phone=False):
    details = [{"id": pid, "reveal_personal_emails": False, "reveal_phone_number": want_phone} for pid in person_ids]
    r = requests.post(
        f"{APOLLO_BASE}/people/bulk_match",
        headers={"x-api-key": api_key, "Content-Type": "application/json", "Cache-Control": "no-cache"},
        json={"details": details},
        timeout=30
    )
    if r.status_code == 200:
        matches = r.json().get("matches", [])
        return {m.get("id", ""): m for m in matches if m}
    return {}


def extract_email(person):
    email = person.get("email", "")
    if email:
        return email
    for e in person.get("contact_emails", []):
        if isinstance(e, dict):
            return e.get("email", "")
    return ""


def extract_phone(person):
    phone = person.get("sanitized_phone", "") or person.get("phone", "")
    if phone:
        return phone
    for p in person.get("phone_numbers", []):
        if isinstance(p, dict):
            return p.get("sanitized_number", "") or p.get("raw_number", "")
    return ""


@app.route("/")
def index():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path) as f:
        return f.read(), 200, {"Content-Type": "text/html"}


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "anthropic_key": bool(ANTHROPIC_KEY), "apollo_key": bool(APOLLO_KEY)})


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
    api_key = request.headers.get("X-Apollo-Key") or APOLLO_KEY
    if not api_key:
        return jsonify({"error": "No Apollo API key provided"}), 401

    job_titles = data.get("jobTitles", [])
    industry_keywords = data.get("industryKeywords", [])
    country = data.get("country", "Australia")
    city = data.get("city", "")
    max_contacts = min(int(data.get("maxContacts", 50)), 500)
    sector_label = data.get("sectorLabel", "")

    if not job_titles:
        return jsonify({"error": "No job titles provided"}), 400

    seen = set()
    raw_people = []
    per_page = 10
    page = 1

    while len(raw_people) < max_contacts:
        result = apollo_search(api_key, job_titles, industry_keywords, country, page=page, per_page=per_page, city=city)
        if not result:
            break
        people = result.get("people", [])
        if not people:
            break
        for p in people:
            if len(raw_people) >= max_contacts:
                break
            pid = p.get("id", "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            raw_people.append(p)
        total_entries = result.get("total_entries", 0)
        if page * per_page >= total_entries or page * per_page >= max_contacts:
            break
        page += 1

    # Return names/companies only — no enrich, no credits used
    results = []
    for p in raw_people:
        pid = p.get("id", "")
        first = p.get("first_name", "")
        # Last name is obfuscated in search — will be revealed on enrich
        name = first
        org = p.get("organization") or {}
        results.append({
            "id": pid,
            "name": name,
            "title": p.get("title", ""),
            "company": org.get("name", ""),
            "domain": "",
            "email": "",
            "phone": "",
            "linkedin": p.get("linkedin_url", ""),
            "city": p.get("city", ""),
            "country": p.get("country", country),
            "sector": sector_label,
        })

    return jsonify({"total": len(results), "contacts": results})


@app.route("/api/enrich", methods=["POST"])
def enrich():
    data = request.json
    api_key = request.headers.get("X-Apollo-Key") or APOLLO_KEY
    if not api_key:
        return jsonify({"error": "No Apollo API key provided"}), 401

    person_ids = data.get("ids", [])
    want_phone = data.get("wantPhone", False)

    if not person_ids:
        return jsonify({"error": "No person IDs provided"}), 400

    # Bulk enrich in batches of 10
    enriched_map = {}
    for i in range(0, len(person_ids), 10):
        batch = person_ids[i:i+10]
        enriched_map.update(apollo_bulk_enrich(api_key, batch, want_phone=want_phone))

    results = {}
    for pid in person_ids:
        e = enriched_map.get(pid, {})
        first = e.get("first_name", "")
        last = e.get("last_name", "")
        name = f"{first} {last}".strip()
        org = e.get("organization") or {}
        results[pid] = {
            "name": name,
            "email": extract_email(e),
            "phone": extract_phone(e) if want_phone else "",
            "linkedin": e.get("linkedin_url", ""),
            "city": e.get("city", ""),
            "domain": org.get("website_url", ""),
        }

    return jsonify({"enriched": results})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
