"""LinkedIn, Naukri, Indeed and Foundit via Apify actors (run-sync endpoint, one call per search)."""
import re
from datetime import datetime, timedelta, timezone

import httpx

from ..config import env
from .base import Job, relevant_title, strip_html

API = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
LINKEDIN_ACTOR = "valig~linkedin-jobs-scraper"
NAUKRI_ACTOR = "valig~naukri-jobs-scraper"
INDEED_ACTOR = "valig~indeed-jobs-scraper"
FOUNDIT_ACTOR = "themineworks~foundit-jobs-scraper"
SKIP_COMPANY = re.compile(r"\b(staffing|consultants?|recruit\w*|manpower|hiring partner)\b", re.I)
RUN_TIMEOUT = 180


def _run(actor: str, payload: dict) -> list[dict]:
    response = httpx.post(API.format(actor=actor), params={"token": env("APIFY_TOKEN")},
                          json=payload, timeout=RUN_TIMEOUT)
    response.raise_for_status()
    return response.json()


def linkedin_item_to_job(item: dict) -> Job | None:
    title, company = item.get("title", ""), item.get("companyName", "")
    if not relevant_title(title) or SKIP_COMPANY.search(company):
        return None
    # Keep the employer's own posting when LinkedIn exposes one.
    url = item.get("applyUrl") or item["url"]
    return Job(company, title, item.get("location", ""), url.split("?")[0] if not item.get("applyUrl") else url,
               "linkedin", item.get("description", ""), (item.get("postedDate") or "")[:10])


def naukri_item_to_job(item: dict) -> Job | None:
    title = item.get("title", "")
    company = (item.get("company") or {}).get("name", "")
    if item.get("consultant") or not relevant_title(title) or SKIP_COMPANY.search(company):
        return None
    experience = item.get("experience") or {}
    created = item.get("createdDate")
    posted = datetime.fromtimestamp(created / 1000, tz=timezone.utc).date().isoformat() if created else ""
    locations = "; ".join(place.get("label", "") for place in item.get("locations") or [])
    description = (item.get("description") or {}).get("full", "")
    skills = ", ".join(item.get("skills", {}).get("other", []) if isinstance(item.get("skills"), dict) else [])
    low, high = experience.get("minimum"), experience.get("maximum")
    return Job(company, title, locations, item["url"], "naukri",
               strip_html(f"{description}\nSkills: {skills}"), posted,
               int(low) if low not in (None, "") else None,
               int(high) if high not in (None, "") else None)


def indeed_item_to_job(item: dict, location: str) -> Job | None:
    """Indeed gives coordinates but no city name, so the searched city stands in."""
    title, company = item.get("title", ""), (item.get("employer") or {}).get("name", "")
    if item.get("expired") or not relevant_title(title) or SKIP_COMPANY.search(company):
        return None
    return Job(company, title, f"{location}, India", item["url"], "indeed",
               (item.get("description") or {}).get("text", ""), (item.get("datePublished") or "")[:10])


def foundit_item_to_job(item: dict) -> Job | None:
    """Foundit search results carry skills and experience but no full description."""
    title, company = item.get("title", ""), item.get("company", "")
    if not item.get("job_id") or not relevant_title(title) or SKIP_COMPANY.search(company):
        return None
    scraped = item.get("scraped_at")
    posted = ""
    if scraped and item.get("posted_days_ago") is not None:
        posted = (datetime.fromisoformat(scraped.replace("Z", "+00:00"))
                  - timedelta(days=item["posted_days_ago"])).date().isoformat()
    description = (f"Skills: {', '.join(item.get('skills') or [])}\n"
                   f"Functions: {', '.join(item.get('functions') or [])}")
    return Job(company, title, item.get("location", ""), item["apply_url"], "foundit", description, posted,
               item.get("experience_min_years"), item.get("experience_max_years"))


def search_linkedin(keywords: str, location: str, limit: int, companies: list[str] | None = None) -> list[Job]:
    payload = {"keywords": keywords, "location": f"{location}, India", "datePosted": "r604800", "limit": limit}
    if companies:
        payload["companyName"] = companies
    return [job for item in _run(LINKEDIN_ACTOR, payload) if (job := linkedin_item_to_job(item))]


def search_naukri(keywords: str, location: str, limit: int, years: int) -> list[Job]:
    payload = {"keywords": keywords, "location": location, "experience": years,
               "jobAge": "7", "sort": "f", "limit": limit}
    return [job for item in _run(NAUKRI_ACTOR, payload) if (job := naukri_item_to_job(item))]


def search_indeed(keywords: str, location: str, limit: int) -> list[Job]:
    payload = {"country": "in", "title": keywords, "location": location, "limit": limit, "datePosted": "7"}
    return [job for item in _run(INDEED_ACTOR, payload) if (job := indeed_item_to_job(item, location))]


def search_foundit(keywords: list[str], locations: list[str], limit: int, years: int) -> list[Job]:
    # Foundit's experience filter is the role's minimum, so allow roles asking a little less.
    payload = {"searchKeywords": keywords, "location": ",".join(locations), "maxJobs": limit,
               "experienceMinYears": max(years - 3, 0), "postedWithinDays": "7"}
    return [job for item in _run(FOUNDIT_ACTOR, payload) if (job := foundit_item_to_job(item))]
