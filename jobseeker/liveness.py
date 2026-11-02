"""Confirms postings are still open. Closed needs an explicit signal; anything else is unverified."""
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import httpx

from . import db
from .config import FEEDS
from .sources.base import USER_AGENT
from .sources.direct import HSBC_API, goldman_roles, goldman_url, juspay_openings

ACTIVE, CLOSED, UNVERIFIED = "active", "closed", "unverified"
BOARD_MAX_AGE_DAYS = 14
MICROSOFT_PAUSE_SECONDS = 1
WORKERS = 6
# Unconfirmed against a real closed Naukri/LinkedIn posting; a miss stays unverified.
BOARD_CLOSED = re.compile(r"no longer accepting applications|job (?:has )?expired|no longer available", re.I)


def from_status(status: int) -> str:
    if status == 200:
        return ACTIVE
    return CLOSED if status in (404, 410) else UNVERIFIED


def check_greenhouse(row: dict, feed: dict, client: httpx.Client) -> str:
    job_id = re.search(r"gh_jid=(\d+)", row["url"]).group(1)
    return from_status(client.get(f"https://boards-api.greenhouse.io/v1/boards/{feed['board']}/jobs/{job_id}").status_code)


def check_workday(row: dict, feed: dict, client: httpx.Client) -> str:
    tenant, dc, site = feed["tenant"], feed["dc"], feed["site"]
    path = row["url"][row["url"].index("/job/"):]
    response = client.get(f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}")
    state = from_status(response.status_code)
    if state == ACTIVE and response.json().get("jobPostingInfo", {}).get("canApply") is False:
        return CLOSED
    return state


def check_microsoft(row: dict, feed: dict, client: httpx.Client) -> str:
    position_id = row["url"].rsplit("/", 1)[1]
    return from_status(client.get("https://apply.careers.microsoft.com/api/pcsx/position_details"
                                  f"?position_id={position_id}&domain=microsoft.com").status_code)


def check_google(row: dict, feed: dict, client: httpx.Client) -> str:
    response = client.get(row["url"])
    if response.status_code != 200:
        return from_status(response.status_code)
    # A closed job falls back to the generic search page with a 200.
    title = re.search(r"<title>(.*?)</title>", response.text, re.S)
    if not title:
        return UNVERIFIED
    return CLOSED if title.group(1).startswith("Jobs search") else ACTIVE


def check_apple(row: dict, feed: dict, client: httpx.Client) -> str:
    response = client.get(row["url"])
    if response.status_code != 200:
        return from_status(response.status_code)
    return ACTIVE if '\\"jobsData\\":{' in response.text else CLOSED


def check_sitemap(row: dict, feed: dict, client: httpx.Client) -> str:
    response = client.get(row["url"])
    if response.status_code != 200:
        return from_status(response.status_code)
    # A closed job redirects to an error page or the site's home page with a 200.
    return ACTIVE if "/job/" in str(response.url) else CLOSED


def check_deloitte(row: dict, feed: dict, client: httpx.Client) -> str:
    if "usijobs.deloitte.com" not in row["url"]:
        return check_sitemap(row, feed, client)
    response = client.get(row["url"])
    # A missing job redirects to the site's error page with a 403.
    if "/Error" in str(response.url):
        return CLOSED
    if response.status_code == 200 and '"JobPosting"' in response.text:
        return ACTIVE
    return UNVERIFIED if response.status_code == 200 else from_status(response.status_code)


def check_juspay(row: dict, feed: dict, client: httpx.Client) -> str:
    job_id = row["url"].rsplit("/", 1)[1]
    openings = juspay_openings(client.get("https://juspay.io/careers").text)
    return ACTIVE if any(item["job_id"] == job_id for item in openings) else CLOSED


def check_hsbc(row: dict, feed: dict, client: httpx.Client) -> str:
    position_id = row["url"].rsplit("/", 1)[1]
    return from_status(client.get(f"{HSBC_API}/{position_id}", params={"domain": "hsbc.com"}).status_code)


def check_board(row: dict, client: httpx.Client) -> str:
    response = client.get(row["url"])
    if response.status_code in (404, 410) or (response.status_code == 200 and BOARD_CLOSED.search(response.text)):
        return CLOSED
    return UNVERIFIED


CHECKERS = {"greenhouse": check_greenhouse, "workday": check_workday, "microsoft": check_microsoft,
            "google": check_google, "apple": check_apple, "sitemap": check_sitemap, "juspay": check_juspay,
            "hsbc": check_hsbc, "deloitte": check_deloitte}


def check_job(row: dict, client: httpx.Client, atlassian_urls: set[str] | None,
              goldman_urls: set[str] | None = None) -> str:
    try:
        feed = FEEDS.get(row["company"]) if row["source"] == "careers" else None
        if feed is None:
            return check_board(row, client)
        if feed["type"] == "atlassian":
            if atlassian_urls is None:
                return UNVERIFIED
            return ACTIVE if row["url"] in atlassian_urls else CLOSED
        if feed["type"] == "goldman":
            if goldman_urls is None:
                return UNVERIFIED
            return ACTIVE if row["url"] in goldman_urls else CLOSED
        return CHECKERS[feed["type"]](row, feed, client)
    except Exception:  # a failed check says nothing about the posting
        return UNVERIFIED


def _atlassian_urls(client: httpx.Client) -> set[str] | None:
    try:
        listings = client.get("https://www.atlassian.com/endpoint/careers/listings").json()
        return {item["applyUrl"].split("/job?")[0] + "/job" for item in listings}
    except Exception:
        return None


def _goldman_urls(client: httpx.Client) -> set[str] | None:
    try:
        return {goldman_url(role) for role in goldman_roles(client)}
    except Exception:
        return None


def expired_board_role(row: dict) -> bool:
    if row["source"] == "careers":
        return False
    return datetime.fromisoformat(row["last_seen"]) < datetime.now() - timedelta(days=BOARD_MAX_AGE_DAYS)


def verify_jobs(conn, seen_before: str | None = None) -> dict:
    """Check roles not seen in this run's listings; a listed role is already known to be live."""
    query = "SELECT * FROM jobs WHERE active_state != 'closed' AND status != 'dismissed'"
    args = ()
    if seen_before:
        query += " AND last_seen < ?"
        args = (seen_before,)
    rows = [dict(r) for r in conn.execute(query, args)]
    with httpx.Client(headers=USER_AGENT, timeout=25, follow_redirects=True) as client:
        atlassian = _atlassian_urls(client) if any(r["company"] == "Atlassian" for r in rows) else None
        goldman = _goldman_urls(client) if any(r["company"] == "Goldman Sachs" for r in rows) else None
        microsoft = [r for r in rows if r["company"] == "Microsoft" and r["source"] == "careers"]
        others = [r for r in rows if r not in microsoft]
        with ThreadPoolExecutor(WORKERS) as pool:
            states = list(pool.map(lambda r: check_job(r, client, atlassian, goldman), others))
        for row in microsoft:  # this site rate limits quickly
            states.append(check_job(row, client, atlassian))
            time.sleep(MICROSOFT_PAUSE_SECONDS)
    counts = {ACTIVE: 0, CLOSED: 0, UNVERIFIED: 0}
    for row, state in zip(others + microsoft, states):
        if state == UNVERIFIED and expired_board_role(row):
            state = CLOSED
        counts[state] += 1
        verified = db.now() if state != UNVERIFIED else row["verified_at"]
        conn.execute("UPDATE jobs SET active_state = ?, verified_at = ? WHERE id = ?", (state, verified, row["id"]))
    return counts
