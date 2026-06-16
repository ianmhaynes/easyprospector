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
  "industryKeywords": ["keyword1", "keyword2"],
  "sectorLabel": "short label e.g. 'Healthcare' or 'SaaS Technology'",
  "explanation": "one sentence summary"
}}

Rules:
- jobTitles: include both abbreviated and full versions (e.g. "CMO" AND "Chief Marketing Officer")
- industryKeywords: 1-3 plain English industry keywords (e.g. "healthcare", "software", "real estate"). Leave empty [] if no industry specified.
- sectorLabel: a SHORT human-readable label for the sector badge (max 3 words). Empty string if no industry specified.
- explanation: one sentence describing what will be searched"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def apollo_search(api_key, job_titles, industry_keywords, country, page=1, per_page=10):
    params = {
        "per_page": per_page,
        "page": page,
    }

    for title in job_titles:
        params.setdefault("person_titles[]", [])
        if isinstance(params["person_titles[]"], list):
            params["person_titles[]"].append(title)

    if country:
        params["person_locations[]"] = [country]

    if industry_keywords:
        params["q_keywords"] = " ".join(industry_keywords)

    r = requests.post(
        f"{APOLLO_BASE}/mixed_people/api_search",
        headers={
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
        },
        json=params,
        timeout=30
    )
    if r.status_code == 200:
        return r.json()
    return None


def apollo_enrich(api_key, person_id, want_email=True, want_phone=False):
    payload = {
        "id": person_id,
        "reveal_personal_emails": False,
        "reveal_phone_number": want_phone,
    }
    r = requests.post(
        f"{APOLLO_BASE}/people/match",
        headers={
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
        },
        json=payload,
        timeout=30
    )
    if r.status_code == 200:
        return r.json().get("person", {})
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
    return jsonify({
        "status": "ok",
        "anthropic_key": bool(ANTHROPIC_KEY),
        "apollo_key": bool(APOLLO_KEY)
    })


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
    max_contacts = min(int(data.get("maxContacts", 50)), 500)
    want_email = data.get("wantEmail", True)
    want_phone = data.get("wantPhone", False)
    sector_label = data.get("sectorLabel", "")

    if not job_titles:
        return jsonify({"error": "No job titles provided"}), 400

    seen = set()
    results = []
    per_page = 10
    page = 1

    while len(results) < max_contacts:
        result = apollo_search(api_key, job_titles, industry_keywords, country, page=page, per_page=per_page)
        if not result:
            break

        people = result.get("people", [])
        if not people:
            break

        for p in people:
            if len(results) >= max_contacts:
                break

            pid = p.get("id", "")
            if not pid or pid in seen:
                continue
            seen.add(pid)

            name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            email = ""
            phone = ""

            if want_email or want_phone:
                enriched = apollo_enrich(api_key, pid, want_email=want_email, want_phone=want_phone)
                if want_email:
                    email = extract_email(enriched)
                if want_phone:
                    phone = extract_phone(enriched)

            linkedin = p.get("linkedin_url", "")

            results.append({
                "id": pid,
                "name": name,
                "title": p.get("title", ""),
                "company": p.get("organization_name", "") or (p.get("organization") or {}).get("name", ""),
                "domain": (p.get("organization") or {}).get("website_url", ""),
                "email": email,
                "phone": phone,
                "linkedin": linkedin,
                "city": p.get("city", ""),
                "country": p.get("country", country),
                "sector": sector_label,
            })

        total_entries = result.get("total_entries", 0)
        if page * per_page >= total_entries or page * per_page >= max_contacts:
            break
        page += 1

    return jsonify({"total": len(results), "contacts": results[:max_contacts]})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
