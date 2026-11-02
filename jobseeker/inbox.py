"""Finds job-application updates in your inbox over IMAP, read-only.

Needs EMAIL_ADDRESS and EMAIL_APP_PASSWORD in .env (for Gmail, an app password).
The folder is opened with EXAMINE and bodies are fetched with BODY.PEEK, so nothing is
marked read, moved or deleted. Only emails that look job-related are stored, in the local database."""
import email
import html
import imaplib
import re
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.policy import default
from email.utils import parseaddr, parsedate_to_datetime

from . import db, llm
from .config import env

FIRST_SCAN_DAYS = 30
MAX_HEADERS = 2000
FETCH_BATCH = 200
BODY_LIMIT = 4000
MAX_LLM_EMAILS = 40

# Hiring platforms and job boards; mail from these is always looked at.
JOB_SENDERS = (
    "greenhouse.io", "greenhouse-mail.io", "myworkday.com", "myworkdayjobs.com", "workday.com", "lever.co",
    "smartrecruiters.com", "icims.com", "eightfold.ai", "avature.net", "successfactors.com", "taleo.net",
    "ashbyhq.com", "jobvite.com", "phenompeople.com", "beamery.com", "naukri.com", "linkedin.com",
    "indeed.com", "foundit.in", "instahyre.com", "hirist.tech", "cutshort.io", "wellfound.com",
    "hackerrank.com", "codility.com", "hackerearth.com",
)
# A bare "offer" is left out: it matches shopping promotions.
JOB_SUBJECT = re.compile(r"applica|applied|applying|interview|assessment|candida|recruit|hiring|position|"
                         r"\brole\b|opening|opportunit|career|\bjobs?\b|job offer|offer letter", re.I)

# Checked against the subject first, then the body; the first match wins.
CATEGORIES = [
    ("Offer", r"offer letter|pleased to offer|job offer|offer of employment|extend (?:you )?an offer"),
    ("Rejection", r"not (?:be )?(?:moving|proceeding|progressing) (?:forward|further)|"
                  r"(?:move|proceed|go) (?:forward )?with other candidates|not been (?:selected|shortlisted)|"
                  r"regret to inform|will not be progressing|no longer (?:under consideration|being considered)|"
                  r"position has been filled|unable to (?:offer you|move forward)|unfortunately"),
    ("Interview", r"invit\w* (?:you )?(?:to|for) (?:an? |the )?(?:interview|call|conversation|discussion)|"
                  r"interview (?:invitation|invite|schedul|confirm|slot|details)|schedule (?:an? |your )?interview|"
                  r"next round|interview on\b"),
    ("Assessment", r"\bassessment\b|coding (?:test|challenge|exercise)|hackerrank|codility|hackerearth|"
                   r"online test|take[- ]home"),
    ("Application received", r"application (?:has been |was )?(?:received|submitted)|"
                             r"thanks? (?:you )?for (?:your )?(?:applying|application|interest)|"
                             r"(?:we(?:'ve| have)|we) received your application|successfully applied|application sent"),
    ("Job alert", r"job alert|jobs? (?:for you|matching|recommended|you may)|new jobs?\b|recommended jobs|"
                  r"is hiring|are hiring"),
    ("Recruiter outreach", r"your profile|came across your|are you open to|interested in (?:a|an|the) "
                           r"(?:role|position|opportunity)|exciting opportunity|reach(?:ing)? out"),
]
CATEGORY_PATTERNS = [(name, re.compile(pattern, re.I)) for name, pattern in CATEGORIES]
LLM_CATEGORIES = [name for name, _ in CATEGORIES] + ["Other"]
# A matched role moves to this status; later emails override earlier ones.
JOB_STATUS = {"Application received": "applied", "Assessment": "assessment", "Interview": "interviewing",
              "Offer": "offer", "Rejection": "rejected"}

EMAIL_PROMPT = """You read one email from a job seeker's inbox. Use only what the email says.
Return JSON: {{"category": one of {categories}, "company": str, "role": str,
"summary": str, "next_step": str, "deadline": str}}
"category" is "Other" when the email is not about a job application, interview or opening.
"company" is the hiring company, not the job board or hiring platform; "" if not stated.
"summary" is one sentence. "next_step" is what the candidate must do, "" if nothing.
"deadline" is a date or time the email gives for that step, "" if none.

FROM: {sender}
SUBJECT: {subject}
BODY:
{body}
"""

class NotConfigured(RuntimeError):
    pass


def configured() -> bool:
    return bool(env("EMAIL_ADDRESS") and env("EMAIL_APP_PASSWORD"))


def body_text(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, UnicodeDecodeError):
        text = part.get_payload(decode=True).decode("utf-8", "replace")
    if part.get_content_type() == "text/html":
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip()[:BODY_LIMIT]


def looks_job_related(sender: str, subject: str) -> bool:
    domain = parseaddr(sender)[1].lower().rpartition("@")[2]
    return any(domain == d or domain.endswith("." + d) for d in JOB_SENDERS) or bool(JOB_SUBJECT.search(subject))


def classify(subject: str, body: str) -> str | None:
    for text in (subject, body):
        for name, pattern in CATEGORY_PATTERNS:
            if pattern.search(text):
                return name
    return None


def match_company(text: str, companies: list[str]) -> str:
    """The longest known company name that appears as whole words in text."""
    for name in sorted(companies, key=len, reverse=True):
        if len(name) >= 3 and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.I):
            return name
    return ""


def match_job(conn, company: str, text: str) -> dict | None:
    """The stored role at this company whose title the email names; the longest title wins."""
    if not company:
        return None
    lowered = text.lower()
    rows = [dict(r) for r in conn.execute("SELECT id, title, status FROM jobs WHERE lower(company) LIKE ?",
                                          (f"%{company.lower()}%",))]
    named = [r for r in rows if r["title"].lower() in lowered]
    return max(named, key=lambda r: len(r["title"])) if named else None


def received_at(message: EmailMessage) -> str:
    try:
        return parsedate_to_datetime(message["Date"]).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return db.now()


def record(conn, messages: list[EmailMessage], companies: list[str], use_llm: bool) -> dict:
    """Stores the job-related messages and moves matched roles to their new status."""
    stats = {"emails": 0, "updated_roles": 0, "llm_failed": False}
    llm_calls = 0
    for message in sorted(messages, key=received_at):
        sender, subject = str(message["From"] or ""), str(message["Subject"] or "")
        message_id = str(message["Message-ID"] or f"{sender}|{subject}|{message['Date']}")
        if conn.execute("SELECT 1 FROM emails WHERE message_id = ?", (message_id,)).fetchone():
            continue
        if not looks_job_related(sender, subject):
            continue
        body = body_text(message)
        category = classify(subject, body)
        details = {"company": "", "role": "", "summary": "", "next_step": "", "deadline": ""}
        if use_llm and not stats["llm_failed"] and llm_calls < MAX_LLM_EMAILS:
            llm_calls += 1
            try:
                answer = llm.complete_json(EMAIL_PROMPT.format(
                    categories=LLM_CATEGORIES, sender=sender, subject=subject, body=body))
                category = answer.get("category") if answer.get("category") in LLM_CATEGORIES else category
                details.update({k: str(answer.get(k) or "") for k in details})
            except Exception:
                # Rules alone for the rest of this scan, e.g. when OpenAI credits run out.
                stats["llm_failed"] = True
        if category in (None, "Other"):
            continue
        searched = f"{parseaddr(sender)[0]} {subject} {body}"
        company = match_company(details["company"], companies) or details["company"] \
            or match_company(searched, companies)
        job = match_job(conn, company, f"{details['role']} {subject} {body}")
        conn.execute(
            "INSERT INTO emails (message_id, received_at, sender, subject, category, company, role, job_id,"
            " summary, next_step, deadline, found_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (message_id, received_at(message), sender, subject, category, company,
             details["role"] or (job["title"] if job else ""), job["id"] if job else None,
             details["summary"] or body[:200], details["next_step"], details["deadline"], db.now()))
        stats["emails"] += 1
        if job and category in JOB_STATUS:
            conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (JOB_STATUS[category], job["id"]))
            stats["updated_roles"] += 1
    return stats


def fetch_messages(since: datetime) -> list[EmailMessage]:
    if not configured():
        raise NotConfigured("Add EMAIL_ADDRESS and EMAIL_APP_PASSWORD to .env to check email.")
    with imaplib.IMAP4_SSL(env("EMAIL_IMAP_HOST") or "imap.gmail.com") as imap:
        imap.login(env("EMAIL_ADDRESS"), env("EMAIL_APP_PASSWORD"))
        imap.select(env("EMAIL_FOLDER") or "INBOX", readonly=True)
        _, data = imap.uid("SEARCH", None, "SINCE", since.strftime("%d-%b-%Y"))
        uids = data[0].split()[-MAX_HEADERS:]
        wanted = []
        for start in range(0, len(uids), FETCH_BATCH):
            batch = b",".join(uids[start:start + FETCH_BATCH])
            _, rows = imap.uid("FETCH", batch, "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            for row in rows:
                if not isinstance(row, tuple):
                    continue
                header = email.message_from_bytes(row[1], policy=default)
                if looks_job_related(str(header["From"] or ""), str(header["Subject"] or "")):
                    wanted.append(re.search(rb"UID (\d+)", row[0]).group(1))
        messages = []
        for start in range(0, len(wanted), FETCH_BATCH):
            _, rows = imap.uid("FETCH", b",".join(wanted[start:start + FETCH_BATCH]), "(BODY.PEEK[])")
            messages += [email.message_from_bytes(row[1], policy=default) for row in rows if isinstance(row, tuple)]
        return messages


def scan(conn, companies: list[str]) -> dict:
    """Checks new mail since the last scan (the last 30 days the first time)."""
    last = conn.execute("SELECT last_scan FROM inbox_state WHERE id = 1").fetchone()
    # IMAP SINCE has day granularity, so step back a day; stored emails are skipped.
    since = (datetime.fromisoformat(last["last_scan"]) - timedelta(days=1) if last
             else datetime.now() - timedelta(days=FIRST_SCAN_DAYS))
    started = db.now()
    known = [r["company"] for r in conn.execute("SELECT DISTINCT company FROM jobs")]
    stats = record(conn, fetch_messages(since), list(dict.fromkeys(companies + known)),
                   use_llm=bool(env("OPENAI_API_KEY")))
    conn.execute("INSERT INTO inbox_state (id, last_scan) VALUES (1, ?) "
                 "ON CONFLICT(id) DO UPDATE SET last_scan = excluded.last_scan", (started,))
    return stats
