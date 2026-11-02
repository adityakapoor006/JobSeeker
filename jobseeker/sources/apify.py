"""LinkedIn and Naukri via Apify actors (run-sync endpoint, one call per search)."""
import re
from datetime import datetime, timezone

import httpx

from ..config import env
from .base import Job, relevant_title, strip_html

API = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
LINKEDIN_ACTOR = "valig~linkedin-jobs-scraper"
NAUKRI_ACTOR = "valig~naukri-jobs-scraper"
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


def search_linkedin(keywords: str, location: str, limit: int, companies: list[str] | None = None) -> list[Job]:
    payload = {"keywords": keywords, "location": f"{location}, India", "datePosted": "r604800", "limit": limit}
    if companies:
        payload["companyName"] = companies
    return [job for item in _run(LINKEDIN_ACTOR, payload) if (job := linkedin_item_to_job(item))]


def search_naukri(keywords: str, location: str, limit: int, years: int) -> list[Job]:
    payload = {"keywords": keywords, "location": location, "experience": years,
               "jobAge": "7", "sort": "f", "limit": limit}
    return [job for item in _run(NAUKRI_ACTOR, payload) if (job := naukri_item_to_job(item))]
