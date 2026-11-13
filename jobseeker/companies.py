"""Checks a company name before it joins the target list.

Evidence comes from three places, strongest first: job listings already in the database,
Wikidata, and finally the AI model's view (OpenAI or Claude) when neither knows the name (common for startups)."""
import difflib
import re

import httpx

from . import llm
from .config import FEEDS

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
# Wikidata asks API clients to identify themselves.
WIKIDATA_HEADERS = {"User-Agent": "JobSeeker/1.0 (https://github.com/adityakapoor006/JobSeeker)"}
COMPANY_WORDS = re.compile(r"compan|corporat|bank|firm|business|conglomerate|enterprise|startup|"
                           r"manufactur|retailer|provider|insur|consult|multinational|subsidiary|employer|"
                           r"platform|e-?commerce|marketplace|delivery|software|payment|fintech|airline|"
                           r"operator|developer|brand|chain|service", re.I)
SUFFIXES = re.compile(r"\b(inc|ltd|limited|llc|plc|corp|corporation|co|pvt|private|group|technologies|"
                      r"technology|india)\b\.?", re.I)
# Only entries plainly described as companies are offered as "did you mean".
SUGGEST_WORDS = re.compile(r"compan|corporat|bank|conglomerate|multinational|enterprise|startup|subsidiary", re.I)
CLOSE_MATCH = 0.8

CHECK_PROMPT = """Is "{name}" a real company that employs people? Answer from what you know; do not guess.
Return JSON: {{"is_company": bool, "official_name": str, "description": str,
"confidence": "high" | "medium" | "low", "did_you_mean": [str]}}
"official_name" is the name it is commonly listed under on job sites, "" if not a company.
"description" is one short line on what it does. Use "low" confidence when you are unsure the
company exists. "did_you_mean" lists up to 3 real companies the user may have meant, for example
when the name is misspelt, otherwise [].
"""


def normalise(name: str) -> str:
    return re.sub(r"\W+", " ", SUFFIXES.sub(" ", name.lower())).strip()


def already_listed(name: str, companies: list[str]) -> str:
    key = normalise(name)
    return next((c for c in companies if normalise(c) == key), "")


def from_listings(name: str, known: dict[str, int]) -> tuple[dict | None, list[str]]:
    """known maps company names seen in job listings to how many roles each has."""
    key = normalise(name)
    exact = [c for c in known if normalise(c) == key]
    if exact:
        best = max(exact, key=lambda c: known[c])
        return {"name": best, "source": "Job listings",
                "evidence": f"{sum(known[c] for c in exact)} roles from this company are already in your database"}, []
    by_key = {normalise(c): c for c in known}
    close = difflib.get_close_matches(key, list(by_key), n=3, cutoff=CLOSE_MATCH)
    return None, [by_key[c] for c in close]


def wikidata_match(name: str, results: list[dict]) -> tuple[dict | None, list[str]]:
    """Picks the Wikidata entry whose label is this name and which is described as a company."""
    companies = [r for r in results if COMPANY_WORDS.search(r.get("description", ""))]
    key = normalise(name)
    for entry in companies:
        if normalise(entry.get("label", "")) == key:
            return {"name": entry["label"], "source": "Wikidata", "evidence": entry["description"]}, []
    return None, [r["label"] for r in companies if SUGGEST_WORDS.search(r["description"])][:3]


def search_wikidata(name: str) -> list[dict]:
    response = httpx.get(WIKIDATA_API, headers=WIKIDATA_HEADERS, timeout=20, params={
        "action": "wbsearchentities", "search": name, "language": "en", "type": "item", "limit": 7,
        "format": "json"})
    response.raise_for_status()
    return response.json().get("search", [])


def check(name: str, companies: list[str], known: dict[str, int]) -> dict:
    """Returns {"status": "listed" | "verified" | "unverified", "match": dict | None, "suggestions": [...],
    "notes": [...]}; only "verified" names should be added without asking."""
    name = name.strip()
    if not normalise(name):
        return {"status": "unverified", "match": None, "suggestions": [], "notes": ["Enter a company name."]}
    listed = already_listed(name, companies)
    if listed:
        return {"status": "listed", "match": {"name": listed}, "suggestions": [], "notes": []}
    for feed_name in FEEDS:
        if normalise(feed_name) == normalise(name):
            return {"status": "verified", "suggestions": [], "notes": [],
                    "match": {"name": feed_name, "source": "Career-site feeds",
                              "evidence": "JobSeeker already reads this company's career site"}}
    notes: list[str] = []
    match, suggestions = from_listings(name, known)
    if not match:
        try:
            match, wiki_suggestions = wikidata_match(name, search_wikidata(name))
            suggestions += wiki_suggestions
        except httpx.HTTPError as exc:
            notes.append(f"Wikidata could not be reached ({type(exc).__name__}).")
    if not match and llm.available():
        try:
            answer = llm.complete_json(CHECK_PROMPT.format(name=name), cheap=True)
            if answer.get("is_company") and answer.get("official_name") and answer.get("confidence") != "low":
                match = {"name": answer["official_name"], "source": "AI model",
                         "evidence": answer.get("description", "") + " (the model's knowledge; not checked elsewhere)"}
            suggestions += [s for s in answer.get("did_you_mean") or [] if isinstance(s, str)]
        except Exception as exc:
            reason = "no credit left" if llm.OUT_OF_CREDIT.search(str(exc)) else type(exc).__name__
            notes.append(f"The AI check couldn't run ({reason}), so only job listings and Wikidata were used.")
    if match and already_listed(match["name"], companies):
        return {"status": "listed", "match": {"name": already_listed(match["name"], companies)},
                "suggestions": [], "notes": notes}
    for s in dict.fromkeys(suggestions):
        if s and already_listed(s, companies):
            notes.append(f"Did you mean {already_listed(s, companies)}? It is already in your list.")
    unique = list(dict.fromkeys(s for s in suggestions if s and not already_listed(s, companies)))
    if match:
        unique = [s for s in unique if normalise(s) != normalise(match["name"])]
    return {"status": "verified" if match else "unverified", "match": match, "suggestions": unique[:5],
            "notes": notes}
