"""Common job shape plus the filters shared by every source."""
import hashlib
import html
import re
from dataclasses import dataclass

USER_AGENT = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
SEARCH_TERMS = ["software engineer", "backend engineer", "AI engineer"]

# Roles the title alone rules out for a senior engineer.
TITLE_EXCLUDE = re.compile(
    r"\b(intern|internship|trainee|graduate|fresher|junior|jr\.?|associate software|apprentice|"
    r"sales|account executive|recruiter|accountant|accounting|marketing|hr |legal|counsel|"
    r"technician|retail|store)\b",
    re.I,
)
TITLE_INCLUDE = re.compile(
    r"\b(engineer|engineering|developer|architect|sde|swe|tech(nical)? lead|staff|principal|"
    r"platform|backend|back-end|full.?stack|software|ai|artificial intelligence|ml|llm|genai|vp|avp)\b",
    re.I,
)


@dataclass
class Job:
    company: str
    title: str
    location: str
    url: str
    source: str
    description: str = ""
    posted_at: str = ""
    exp_min: int | None = None
    exp_max: int | None = None

    @property
    def id(self) -> str:
        return hashlib.sha1(self.url.encode()).hexdigest()[:16]


def strip_html(text: str) -> str:
    text = html.unescape(html.unescape(text or ""))
    text = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def relevant_title(title: str) -> bool:
    return bool(TITLE_INCLUDE.search(title)) and not TITLE_EXCLUDE.search(title)


def in_locations(location: str, terms: list[str], include_remote: bool) -> bool:
    loc = (location or "").lower()
    if any(term in loc for term in terms):
        return True
    return include_remote and "remote" in loc and "india" in loc


def experience_range(text: str) -> tuple[int | None, int | None]:
    """Pull the required years out of JD text, e.g. '5-8 years' or '8+ years'."""
    text = (text or "").lower()
    span = re.search(r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*\+?\s*(?:years|yrs)", text)
    if span:
        return int(span.group(1)), int(span.group(2))
    # Needs the word "experience" nearby, so company history like "25 years" doesn't count.
    pattern = r"(\d{1,2})\s*\+?\s*(?:years|yrs)[^.\n]{0,40}experience"
    floors = [int(n) for n in re.findall(pattern, text) if int(n) <= 20]
    return (max(floors), None) if floors else (None, None)


def fits_experience(job: Job, years: int) -> bool:
    """Drop roles clearly outside the candidate's level; unknown stays in."""
    low, high = job.exp_min, job.exp_max
    if low is None and job.description:
        low, high = experience_range(job.description)
        job.exp_min, job.exp_max = low, high
    if low is not None and low > years + 4:
        return False
    if high is not None and high < years - 3:
        return False
    return True
