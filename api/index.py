from flask import Flask, request, jsonify
import requests
import json
import os
import anthropic
from urllib.parse import urlencode

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
- industryKeywords: MAX 1 short industry keyword (e.g. "healthcare", "software", "hospitality", "finance"). Use simple common words only — avoid phrases like "financial services" or "wealth management", use "finance" instead. Leave empty [] if no industry specified or if multiple unrelated industries are requested.
- sectorLabel: a SHORT human-readable label for the sector badge (max 3 words). Empty string if no industry specified.
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


def apollo_search(api_key, job_titles, industry_keywords, country, page=1, per_page=10, city=''):
    # Apollo requires array params in URL query string, not JSON body
    parts = []
    for title in job_titles:
        parts.append(f"person_titles[]={requests.utils.quote(title)}")
    if country:
        if city:
            parts.append(f"person_locations[]={requests.utils.quote(city + ", " + country)}")
        else:
            parts.append(f"person_locations[]={requests.utils.quote(country)}")
    # q_keywords removed - titles + location filter is sufficient
    parts.append(f"per_page={per_page}")
    parts.append(f"page={page}")

    url = f"{APOLLO_BASE}/mixed_people/api_search?{'&'.join(parts)}"

    r = requests.post(
        url,
        headers={
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
        },
        timeout=30
    )
    if r.status_code == 200:
        return r.json()
    return None


def apollo_enrich(api_key, person_id, want_phone=False):
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
    city = data.get("city", "")
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
        result = apollo_search(api_key, job_titles, industry_keywords, country, page=page, per_page=per_page, city=city)
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

            # Only enrich if email/phone needed — avoids timeout on large searches
            enriched = apollo_enrich(api_key, pid, want_phone=want_phone) if (want_email or want_phone) else {}

            first = enriched.get("first_name", "") or p.get("first_name", "")
            last = enriched.get("last_name", "")
            name = f"{first} {last}".strip()

            email = extract_email(enriched) if want_email else ""
            phone = extract_phone(enriched) if want_phone else ""

            org = enriched.get("organization") or p.get("organization") or {}
            results.append({
                "id": pid,
                "name": name,
                "title": enriched.get("title", "") or p.get("title", ""),
                "company": enriched.get("organization_name", "") or org.get("name", ""),
                "domain": org.get("website_url", ""),
                "email": email,
                "phone": phone,
                "linkedin": enriched.get("linkedin_url", "") or p.get("linkedin_url", ""),
                "city": enriched.get("city", "") or p.get("city", ""),
                "country": enriched.get("country", "") or p.get("country", country),
                "sector": sector_label,
            })

        total_entries = result.get("total_entries", 0)
        if page * per_page >= total_entries or page * per_page >= max_contacts:
            break
        page += 1

    return jsonify({"total": len(results), "contacts": results[:max_contacts]})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
