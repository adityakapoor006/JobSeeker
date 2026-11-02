"""Paths, defaults and the user-editable settings file (data/settings.json)."""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
RESUME_DIR = DATA_DIR / "resume"
DB_PATH = DATA_DIR / "jobseeker.db"
SETTINGS_PATH = DATA_DIR / "settings.json"

load_dotenv(ROOT / ".env")

# Direct feed for each company that has a working public one. Anything
# without an entry is covered through LinkedIn/Naukri only. The user chose on
# 2026-11-02 to run the Google, Microsoft, HSBC, Goldman Sachs and Deloitte USI
# feeds even though robots.txt disallows them; add no other disallowed feed
# without asking.
# "partial" means LinkedIn/Naukri are still searched by name.
FEEDS: dict[str, dict] = {
    "HSBC": {"type": "hsbc"},
    "Goldman Sachs": {"type": "goldman"},
    "Standard Chartered": {"type": "sitemap", "base": "https://jobs.standardchartered.com"},
    # South Asia site from its sitemap, plus the USI site (India offices of Deloitte US).
    "Deloitte": {"type": "deloitte", "base": "https://southasiacareers.deloitte.com"},
    "Nestle": {"type": "sitemap", "base": "https://jobdetails.nestle.com"},
    "ANZ Bank": {"type": "sitemap", "base": "https://careers.anz.com"},
    "Under Armour": {"type": "sitemap", "base": "https://careers.underarmour.com"},
    "Cargill": {"type": "sitemap", "base": "https://careers.cargill.com"},
    "Juspay": {"type": "juspay"},
    "Deepwatch": {"type": "greenhouse", "board": "deepwatchindia"},
    "Google": {"type": "google"},
    "Microsoft": {"type": "microsoft"},
    "Apple": {"type": "apple"},
    "Atlassian": {"type": "atlassian"},
    "Stripe": {"type": "greenhouse", "board": "stripe"},
    "Databricks": {"type": "greenhouse", "board": "databricks"},
    "Salesforce": {"type": "workday", "tenant": "salesforce", "dc": "wd12", "site": "External_Career_Site"},
    "Wells Fargo": {"type": "workday", "tenant": "wf", "dc": "wd1", "site": "WellsFargoJobs"},
    "Capital One": {"type": "workday", "tenant": "capitalone", "dc": "wd12", "site": "Capital_One"},
    "BlackRock": {"type": "workday", "tenant": "blackrock", "dc": "wd1", "site": "BlackRock_Professional"},
    "Bank of America": {"type": "workday", "tenant": "ghr", "dc": "wd1", "site": "Lateral-US"},
    "Vanguard": {"type": "workday", "tenant": "vanguard", "dc": "wd5", "site": "vanguard_external"},
    "Merck": {"type": "workday", "tenant": "msd", "dc": "wd5", "site": "SearchJobs"},
    "Shell": {"type": "workday", "tenant": "shell", "dc": "wd3", "site": "ShellCareers"},
    "Kraft Heinz": {"type": "workday", "tenant": "heinz", "dc": "wd1", "site": "KraftHeinz_Careers"},
    "Citibank": {"type": "workday", "tenant": "citi", "dc": "wd5", "site": "2"},
    "Morgan Stanley": {"type": "workday", "tenant": "ms", "dc": "wd5", "site": "External"},
    "Maersk": {"type": "workday", "tenant": "maersk", "dc": "wd3", "site": "Maersk_Careers"},
    "The Standard": {"type": "workday", "tenant": "standard", "dc": "wd1", "site": "Search"},
}

DEFAULT_SETTINGS = {
    "locations": ["Bengaluru", "Gurugram", "Pune"],
    "include_remote_india": True,
    "years_experience": 8,
    "companies": [
        "Google", "Microsoft", "Salesforce", "Atlassian", "HSBC", "Morgan Stanley",
        "Wells Fargo", "Goldman Sachs", "Juspay", "Citibank", "Standard Chartered",
        "Databricks", "Shell", "Deloitte", "BlackRock", "Vanguard", "Nestle", "Maersk",
        "Cargill", "Kraft Heinz", "Under Armour", "Deepwatch", "The Standard", "Merck",
        "Bank of America", "ANZ Bank", "Capital One", "Walmart", "Stripe", "Apple",
    ],
    # Search beyond the list too (requirement 2b).
    "search_all_companies": True,
    "max_results_per_search": 25,
    "max_results_per_company": 60,
    # Caps LLM spend per run; the best keyword matches are scored first.
    "max_jobs_to_score": 150,
    "max_tailored_per_run": 15,
}

# Same city under its other names, used when matching a posting's location.
CITY_ALIASES = {
    "bengaluru": ["bengaluru", "bangalore", "blr"],
    "gurugram": ["gurugram", "gurgaon", "ggn"],
    "pune": ["pune"],
    "hyderabad": ["hyderabad"],
    "mumbai": ["mumbai", "bombay"],
    "chennai": ["chennai", "madras"],
    "noida": ["noida"],
    "delhi": ["delhi"],
}


def load_settings() -> dict:
    saved = json.loads(SETTINGS_PATH.read_text()) if SETTINGS_PATH.exists() else {}
    return {**DEFAULT_SETTINGS, **saved}


def save_settings(settings: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2))


def location_terms(settings: dict) -> list[str]:
    terms: list[str] = []
    for loc in settings["locations"]:
        key = loc.strip().lower()
        terms.extend(CITY_ALIASES.get(key, [key]))
    return terms


def env(name: str) -> str:
    return os.environ.get(name, "").strip()
