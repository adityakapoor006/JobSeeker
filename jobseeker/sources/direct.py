"""Company career-site feeds. Each returns postings in the configured locations."""
import html
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote, unquote

import httpx

from .base import SEARCH_TERMS, USER_AGENT, Job, in_locations, relevant_title, strip_html

TIMEOUT = 30
MICROSOFT_PAUSE_SECONDS = 1
RATE_LIMIT_BACKOFF_SECONDS = (15, 45)
WORKDAY_PAGE = 20
WORKDAY_MAX_RESULTS = 200
HSBC_API = "https://portal.careers.hsbc.com/api/apply/v2/jobs"
HSBC_PAGE = 10
HSBC_MAX_RESULTS = 50
GOLDMAN_API = "https://api-higher.gs.com/gateway/api/v1/graphql"
GOLDMAN_PAGE = 50
GOLDMAN_MAX_PAGES = 10
GOLDMAN_HEADERS = {"Origin": "https://higher.gs.com", "Referer": "https://higher.gs.com/results"}
USI_BASE = "https://usijobs.deloitte.com/en_US/careersUSI"
USI_PAGE = 10  # the site ignores larger page sizes
USI_MAX_RESULTS = 100
GOLDMAN_QUERY = """query GetRoles($searchQueryInput: RoleSearchQueryInput!) {
  roleSearch(searchQueryInput: $searchQueryInput) {
    items { jobTitle status descriptionHtml locations { city country } externalSource { sourceId } }
  }
}"""


def _client() -> httpx.Client:
    return httpx.Client(headers=USER_AGENT, timeout=TIMEOUT, follow_redirects=True)


def _date(timestamp) -> str:
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).date().isoformat()


def fetch_greenhouse(company: str, feed: dict, terms: list[str], remote: bool, limit: int) -> list[Job]:
    with _client() as client:
        data = client.get(f"https://boards-api.greenhouse.io/v1/boards/{feed['board']}/jobs?content=true").json()
    jobs = []
    for item in data["jobs"]:
        location = item["location"]["name"]
        if relevant_title(item["title"]) and in_locations(location, terms, remote):
            jobs.append(Job(company, item["title"], location, item["absolute_url"], "careers",
                            strip_html(item.get("content", "")), (item.get("first_published") or "")[:10]))
    return jobs[:limit]


def fetch_workday(company: str, feed: dict, terms: list[str], remote: bool, limit: int) -> list[Job]:
    tenant, dc, site = feed["tenant"], feed["dc"], feed["site"]
    base = f"https://{tenant}.{dc}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}"
    seen: dict[str, dict] = {}
    # Workday needs every search word to match, so "engineer pune" can miss what "pune" finds.
    queries = [*terms, "India", *(f"{term} {city}" for term in SEARCH_TERMS for city in terms)]
    with _client() as client:
        for query in queries:
            for offset in range(0, WORKDAY_MAX_RESULTS, WORKDAY_PAGE):
                body = {"appliedFacets": {}, "limit": WORKDAY_PAGE, "offset": offset, "searchText": query}
                response = client.post(api + "/jobs", json=body)
                if response.status_code != 200:
                    break
                postings = response.json().get("jobPostings", [])
                for posting in postings:
                    if relevant_title(posting["title"]):
                        seen.setdefault(posting["externalPath"], posting)
                if len(postings) < WORKDAY_PAGE:
                    break
        jobs = []
        for path in list(seen)[:limit * 2]:
            response = client.get(api + path)
            if response.status_code != 200:
                continue
            info = response.json()["jobPostingInfo"]
            # The list view can say "2 Locations", so check the full posting.
            location = "; ".join([info.get("location", "")] + info.get("additionalLocations", []))
            if in_locations(location, terms, remote):
                jobs.append(Job(company, info["title"], location,
                                info.get("externalUrl") or f"{base}/{site}{path}", "careers",
                                strip_html(info.get("jobDescription", "")), info.get("startDate", "")))
    return jobs[:limit]


def _microsoft_json(client: httpx.Client, url: str) -> dict:
    """This API answers 429 with an HTML body when called too quickly."""
    response = client.get(url)
    for wait in RATE_LIMIT_BACKOFF_SECONDS:
        if response.status_code != 429:
            break
        time.sleep(wait)
        response = client.get(url)
    time.sleep(MICROSOFT_PAUSE_SECONDS)
    response.raise_for_status()
    return response.json()


def fetch_microsoft(company: str, feed: dict, terms: list[str], remote: bool, limit: int) -> list[Job]:
    base = "https://apply.careers.microsoft.com"
    found: dict[int, dict] = {}
    with _client() as client:
        for term in SEARCH_TERMS:
            url = f"{base}/api/pcsx/search?domain=microsoft.com&query={quote(term)}&location=India&start=0"
            for position in _microsoft_json(client, url)["data"].get("positions", []):
                location = "; ".join(position.get("locations", []))
                if relevant_title(position["name"]) and in_locations(location, terms, remote):
                    found.setdefault(position["id"], position)
        jobs = []
        for position_id, position in list(found.items())[:limit]:
            detail = _microsoft_json(client, f"{base}/api/pcsx/position_details?position_id={position_id}"
                                             "&domain=microsoft.com")["data"]
            jobs.append(Job(company, position["name"], "; ".join(position["locations"]),
                            f"{base}{position['positionUrl']}", "careers",
                            strip_html(detail.get("jobDescription", "")), _date(position["postedTs"])))
    return jobs


def parse_google_page(page: str) -> list[list]:
    match = re.search(r"AF_initDataCallback\(\{key: 'ds:1'.*?data:(.*?), sideChannel", page, re.S)
    return json.loads(match.group(1))[0] or [] if match else []


def fetch_google(company: str, feed: dict, terms: list[str], remote: bool, limit: int) -> list[Job]:
    base = "https://www.google.com/about/careers/applications/jobs/results"
    found: dict[str, Job] = {}
    with _client() as client:
        for term in SEARCH_TERMS:
            page = client.get(f"{base}?location=India&q={quote(term)}").text
            for row in parse_google_page(page):
                job_id, title = row[0], row[1]
                location = "; ".join(place[0] for place in row[9] or [])
                if not relevant_title(title) or not in_locations(location, terms, remote):
                    continue
                parts = [row[i][1] for i in (19, 3, 4, 10) if row[i] and len(row[i]) > 1 and row[i][1]]
                # Requested format: direct careers.google.com link, which redirects to the JD.
                found.setdefault(job_id, Job(company, title, location,
                                             f"https://careers.google.com/jobs/results/{job_id}/",
                                             "careers", strip_html("\n".join(parts)),
                                             _date(row[12][0]) if row[12] else ""))
    return list(found.values())[:limit]


def _apple_data(page: str) -> dict:
    raw = re.search(r'__staticRouterHydrationData = JSON\.parse\("(.*?)"\);', page, re.S).group(1)
    return json.loads(json.loads(f'"{raw}"'))["loaderData"]


def fetch_apple(company: str, feed: dict, terms: list[str], remote: bool, limit: int) -> list[Job]:
    base = "https://jobs.apple.com/en-in"
    jobs: dict[str, Job] = {}
    with _client() as client:
        for term in SEARCH_TERMS:
            page = client.get(f"{base}/search?location=india-INDC&search={quote(term)}").text
            for item in _apple_data(page)["search"]["searchResults"]:
                title = item["postingTitle"]
                location = "; ".join(place.get("name", "") for place in item.get("locations", []))
                if not relevant_title(title) or not in_locations(location, terms, remote):
                    continue
                url = f"{base}/details/{item['positionId']}/{item['transformedPostingTitle']}"
                if url in jobs or len(jobs) >= limit:
                    continue
                data = _apple_data(client.get(url).text)["jobDetails"]["jobsData"]
                text = "\n".join(str(data.get(key) or "") for key in
                                 ("jobSummary", "description", "minimumQualifications", "preferredQualifications"))
                jobs[url] = Job(company, title, location, url, "careers", strip_html(text),
                                (data.get("postDateInGMT") or "")[:10])
    return list(jobs.values())


def fetch_atlassian(company: str, feed: dict, terms: list[str], remote: bool, limit: int) -> list[Job]:
    with _client() as client:
        listings = client.get("https://www.atlassian.com/endpoint/careers/listings").json()
    jobs = []
    for item in listings:
        location = "; ".join(item.get("locations", []))
        if relevant_title(item["title"]) and in_locations(location, terms, remote):
            text = "\n".join(item.get(key) or "" for key in ("overview", "responsibilities", "qualifications"))
            url = item["applyUrl"].split("/job?")[0] + "/job"
            jobs.append(Job(company, item["title"], location, url, "careers", strip_html(text)))
    return jobs[:limit]


def fetch_hsbc(company: str, feed: dict, terms: list[str], remote: bool, limit: int) -> list[Job]:
    found: dict[int, dict] = {}
    with _client() as client:
        for term in SEARCH_TERMS:
            for place in ("India", *terms):
                for start in range(0, HSBC_MAX_RESULTS, HSBC_PAGE):
                    params = {"domain": "hsbc.com", "start": start, "num": HSBC_PAGE, "query": term, "location": place}
                    positions = client.get(HSBC_API, params=params).json()["positions"]
                    for position in positions:
                        location = "; ".join(position.get("locations", []))
                        if relevant_title(position["name"]) and in_locations(location, terms, remote):
                            found.setdefault(position["id"], position)
                    if len(positions) < HSBC_PAGE:
                        break
        jobs = []
        for position_id, position in list(found.items())[:limit]:
            detail = client.get(f"{HSBC_API}/{position_id}", params={"domain": "hsbc.com"}).json()
            jobs.append(Job(company, position["name"].strip(), "; ".join(position["locations"]),
                            position["canonicalPositionUrl"], "careers",
                            strip_html(detail.get("job_description", "")), _date(position["t_create"])))
    return jobs


def goldman_url(role: dict) -> str:
    return f"https://higher.gs.com/roles/{role['externalSource']['sourceId']}"


def goldman_roles(client: httpx.Client) -> list[dict]:
    """Every posted India role, so a role missing from this list has closed."""
    india = [{"filterCategoryType": "LOCATION", "filters": [{"filter": "India", "subFilters": []}]}]
    roles: list[dict] = []
    for page in range(GOLDMAN_MAX_PAGES):
        search = {"page": {"pageSize": GOLDMAN_PAGE, "pageNumber": page}, "filters": india, "searchTerm": "",
                  "sort": {"sortStrategy": "RELEVANCE", "sortOrder": "DESC"},
                  "experiences": ["EARLY_CAREER", "PROFESSIONAL"]}
        body = {"query": GOLDMAN_QUERY, "variables": {"searchQueryInput": search}}
        items = client.post(GOLDMAN_API, json=body, headers=GOLDMAN_HEADERS).json()["data"]["roleSearch"]["items"]
        roles += items
        if len(items) < GOLDMAN_PAGE:
            break
    if not roles:
        raise ValueError("Goldman Sachs listing is empty")  # reported as a failure, not as no openings
    return [role for role in roles if role["status"] == "POSTED"]


def fetch_goldman(company: str, feed: dict, terms: list[str], remote: bool, limit: int) -> list[Job]:
    with _client() as client:
        roles = goldman_roles(client)
    jobs = []
    for role in roles:
        title = " ".join(role["jobTitle"].split())
        location = "; ".join(f"{place['city']}, {place['country']}" for place in role["locations"] if place["city"])
        if relevant_title(title) and in_locations(location, terms, remote):
            jobs.append(Job(company, title, location, goldman_url(role), "careers",
                            strip_html(role.get("descriptionHtml", ""))))
    return jobs[:limit]


def sitemap_job_urls(text: str) -> list[str]:
    """Job links from a sitemap, newest first; the posting id is the last number in the URL."""
    urls = [html.unescape(url) for url in re.findall(r"<loc>(.*?)</loc>", text) if "/job/" in url]
    return sorted(urls, key=lambda url: int(re.findall(r"\d+", url)[-1]), reverse=True)


def url_slug(url: str) -> str:
    return re.sub(r"[-/]", " ", unquote(url.split("/job/", 1)[1]))


def job_page_text(page: str) -> str:
    data = re.search(r'<script[^>]+ld\+json[^>]*>(.*?)</script>', page, re.S)
    if data:
        return strip_html(json.loads(data.group(1), strict=False).get("description", ""))
    start = page.find('class="jobDisplay')
    body = page[page.find(">", start) + 1:] if start >= 0 else page
    return strip_html(re.sub(r"<(script|style)[\s\S]*?</\1>", " ", body))


def parse_job_page(company: str, url: str, page: str, terms: list[str]) -> Job:
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S).group(1).strip()
    title = html.unescape(re.sub(rf"\s+(Job Details\s*\|.*|at {re.escape(company)})$", "", title))
    slug = url_slug(url).lower()
    city = next((term for term in terms if term in slug), "remote")
    return Job(company, title, f"{city.title()}, India", url, "careers", job_page_text(page))


def fetch_sitemap(company: str, feed: dict, terms: list[str], remote: bool, limit: int) -> list[Job]:
    """For sites whose search is blocked or unreliable; each job URL holds the city and title."""
    jobs = []
    with _client() as client:
        urls = sitemap_job_urls(client.get(f"{feed['base']}/sitemap.xml").text)
        if not urls:
            raise ValueError("sitemap has no jobs")  # reported as a failure, not as no openings
        for url in urls:
            slug = url_slug(url)
            if len(jobs) >= limit:
                break
            if not relevant_title(slug) or not in_locations(slug, terms, remote):
                continue
            try:
                response = client.get(url)
            except httpx.TimeoutException:  # one slow page shouldn't lose the rest
                continue
            if response.status_code == 200 and "/job/" in str(response.url):
                job = parse_job_page(company, url, response.text, terms)
                if relevant_title(job.title):
                    jobs.append(job)
    return jobs


def usi_cards(page: str) -> list[tuple[str, str, str]]:
    """(url, title, location) per search result; location can read "Multiple Locations"."""
    cards = re.findall(r'<h3 class="article__header__text__title[^>]*>\s*<a href="([^"]+)"[^>]*>\s*(.*?)\s*</a>'
                       r'.*?<div class="article__header__text__subtitle">(.*?)</div>', page, re.S)
    return [(url, html.unescape(title), html.unescape(re.findall(r"<span>(.*?)</span>", sub, re.S)[-1].strip()))
            for url, title, sub in cards]


def usi_locations(page: str, fallback: str) -> str:
    block = re.search(r'article__header--locations[^>]*>(.*?)</div>\s*</div>', page, re.S)
    places = re.findall(r'<p class="paragraph">(.*?)</p>', block.group(1)) if block else []
    return "; ".join(html.unescape(place.strip()) for place in places) or fallback


def fetch_usi(company: str, terms: list[str], remote: bool, limit: int) -> list[Job]:
    candidates: dict[str, tuple[str, str]] = {}
    with _client() as client:
        for term in SEARCH_TERMS:
            for offset in range(0, USI_MAX_RESULTS, USI_PAGE):
                page = client.get(f"{USI_BASE}/SearchJobs/{quote(term)}",
                                  params={"jobRecordsPerPage": USI_PAGE, "jobOffset": offset}).text
                cards = usi_cards(page)
                for url, title, location in cards:
                    if relevant_title(title) and (location == "Multiple Locations" or in_locations(location, terms, remote)):
                        candidates.setdefault(url, (title, location))
                if len(cards) < USI_PAGE:
                    break
        jobs = []
        for url, (title, location) in list(candidates.items())[:limit * 2]:
            response = client.get(url)
            if response.status_code != 200:
                continue
            location = usi_locations(response.text, location)
            if in_locations(location, terms, remote):
                posted = re.search(r'"datePosted":\s*"([\d-]+)"', response.text)
                jobs.append(Job(company, title, location, url, "careers", job_page_text(response.text),
                                posted.group(1) if posted else ""))
    return jobs[:limit]


def fetch_deloitte(company: str, feed: dict, terms: list[str], remote: bool, limit: int) -> list[Job]:
    """Two sites: South Asia (Deloitte India) and USI (India offices of Deloitte US)."""
    return fetch_sitemap(company, feed, terms, remote, limit) + fetch_usi(company, terms, remote, limit)


def _astro_value(value):
    """Astro stores page props as [type, value] pairs: type 1 is a list, 0 a plain value."""
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
        return [_astro_value(item) for item in value[1]] if value[0] == 1 else _astro_value(value[1])
    if isinstance(value, dict):
        return {key: _astro_value(item) for key, item in value.items()}
    return value


def juspay_openings(page: str) -> list[dict]:
    props = re.search(r'component-url="/_astro/JobListings[^"]+"[^>]*?props="([^"]+)"', page).group(1)
    return [job for job in _astro_value(json.loads(html.unescape(props))["jobData"]) if job.get("opening_status")]


def fetch_juspay(company: str, feed: dict, terms: list[str], remote: bool, limit: int) -> list[Job]:
    with _client() as client:
        openings = juspay_openings(client.get("https://juspay.io/careers").text)
    jobs = []
    for item in openings:
        title, location = item["job_title"].strip(), item.get("job_location", "")
        if relevant_title(title) and in_locations(location, terms, remote):
            jobs.append(Job(company, title, location, f"https://juspay.io/careers/{item['job_id']}", "careers",
                            strip_html(item.get("job_description_career", ""))))
    return jobs[:limit]


FETCHERS = {
    "sitemap": fetch_sitemap,
    "deloitte": fetch_deloitte,
    "juspay": fetch_juspay,
    "hsbc": fetch_hsbc,
    "goldman": fetch_goldman,
    "greenhouse": fetch_greenhouse,
    "workday": fetch_workday,
    "microsoft": fetch_microsoft,
    "google": fetch_google,
    "apple": fetch_apple,
    "atlassian": fetch_atlassian,
}
