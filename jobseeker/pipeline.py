"""One full run: analyse resume, collect, score, bucket, tailor, report."""
import fcntl
import json
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime

import httpx

from . import db, inbox, liveness, llm
from .render import markdown_to_docx
from .config import DATA_DIR, FEEDS, OUTPUT_DIR, env, load_settings, location_terms
from .resume import load_resume
from .sources import apify
from .sources.base import Job, fits_experience, in_locations
from .sources.direct import FETCHERS

DESCRIPTION_LIMIT = 6000
SCORE_WORKERS = 6
NUMBER = re.compile(r"\d+(?:\.\d+)?")
TERM = re.compile(r"\b[A-Z][A-Za-z0-9+#.-]{1,}\b")
MUST_WEIGHT, NICE_WEIGHT = 3, 1
STATUS_CREDIT = {"met": 1.0, "partial": 0.5, "missing": 0.0}
EVIDENCE_OVERLAP = 0.8
OPEN_LABEL = {"active": "confirmed", "unverified": "unconfirmed"}
INTERNAL_HEADING = "ADDITIONAL VERIFIED DETAIL"
LOCK_PATH = DATA_DIR / "run.lock"
BOARD_WORKERS = 4  # Apify's free plan runs at most 5 actor jobs at once


class RunInProgress(RuntimeError):
    pass


@contextmanager
def run_lock():
    """One run at a time: overlapping runs exceed Apify's concurrent-job limit."""
    DATA_DIR.mkdir(exist_ok=True)
    with open(LOCK_PATH, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunInProgress("Another JobSeeker run is already in progress. Try again when it finishes.") from None
        yield


def describe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def bucket_for(score: int) -> str:
    if score >= 80:
        return "High"
    if score >= 60:
        return "Medium"
    if score >= 40:
        return "Stretch"
    return "Excluded"


def get_analysis(conn, resume: str, resume_hash: str) -> dict:
    """Runs once per resume version."""
    row = conn.execute("SELECT analysis_json FROM analysis WHERE resume_hash = ?", (resume_hash,)).fetchone()
    if row:
        return json.loads(row["analysis_json"])
    analysis = llm.complete_json(llm.ANALYSIS_PROMPT + resume)
    conn.execute("INSERT INTO analysis VALUES (?, ?, ?)", (resume_hash, db.now(), json.dumps(analysis)))
    return analysis


def collect(settings: dict, roles: list[str]) -> tuple[list[Job], dict, list[str]]:
    """Gather from every source. A failing source is reported, never fatal."""
    terms = location_terms(settings)
    remote = settings["include_remote_india"]
    limit = settings["max_results_per_search"]
    years = settings["years_experience"]
    jobs: list[Job] = []
    counts: dict[str, int | None] = {}
    errors: list[str] = []

    def direct(company: str):
        feed = FEEDS[company]
        return company, FETCHERS[feed["type"]](company, feed, terms, remote, settings["max_results_per_company"])

    direct_companies = [c for c in settings["companies"] if c in FEEDS]
    with ThreadPoolExecutor(6) as pool:
        futures = {c: pool.submit(direct, c) for c in direct_companies}
    for company, future in futures.items():
        try:
            _, found = future.result()
            counts[company] = len(found)
            jobs += found
        except Exception as exc:  # one broken site must not stop the run
            counts[company] = None
            errors.append(f"{company} careers feed failed: {describe_error(exc)}")

    if not env("APIFY_TOKEN"):
        errors.append("LinkedIn, Naukri, Indeed and Foundit skipped: APIFY_TOKEN is not set")
        return jobs, counts, errors

    uncovered = [c for c in settings["companies"] if c not in FEEDS or FEEDS[c].get("partial")]
    searches = []
    for location in settings["locations"]:
        if uncovered:
            searches.append(("LinkedIn", lambda loc=location: apify.search_linkedin(
                "software engineer", loc, limit, uncovered)))
        if settings["search_all_companies"]:
            for role in roles[:3]:
                searches.append(("LinkedIn", lambda r=role, loc=location: apify.search_linkedin(r, loc, limit)))
                searches.append(("Naukri", lambda r=role, loc=location: apify.search_naukri(r, loc, limit, years)))
                searches.append(("Indeed", lambda r=role, loc=location: apify.search_indeed(r, loc, limit)))
    if settings["search_all_companies"] and settings["foundit_max_jobs"]:
        # Foundit charges per job, so it gets one capped search across every city.
        searches.append(("Foundit", lambda: apify.search_foundit(
            roles[:3], settings["locations"], settings["foundit_max_jobs"], years)))
    with ThreadPoolExecutor(BOARD_WORKERS) as pool:
        futures = [(name, pool.submit(fn)) for name, fn in searches]
    for name, future in futures:
        try:
            jobs += future.result()
        except Exception as exc:
            errors.append(f"{name} search failed: {describe_error(exc)}")

    for company in uncovered:
        counts[company] = sum(_same_company(j.company, company) for j in jobs)
    return jobs, counts, errors


def _same_company(found: str, wanted: str) -> bool:
    return wanted.lower().replace(" bank", "") in found.lower()


def company_openings(conn, companies: list[str]) -> dict[str, list[dict]]:
    """Every open role for each target company, scored or not, best fit first."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE active_state != 'closed' AND status != 'dismissed'"
        " ORDER BY COALESCE(fit_score, -1) DESC, title")]
    return {company: [r for r in rows if _same_company(r["company"], company)] for company in companies}


def _dedupe_key(job: Job) -> tuple[str, str]:
    return re.sub(r"\W+", "", job.company.lower()), re.sub(r"\W+", "", job.title.lower())


def filter_jobs(jobs: list[Job], settings: dict) -> list[Job]:
    """Keep jobs at the right level and place; careers-site copies beat board copies."""
    terms = location_terms(settings)
    remote = settings["include_remote_india"]
    best: dict[tuple[str, str], Job] = {}
    for job in jobs:
        if not in_locations(job.location, terms, remote) or not fits_experience(job, settings["years_experience"]):
            continue
        key = _dedupe_key(job)
        if key not in best or (job.source == "careers" and best[key].source != "careers"):
            best[key] = job
    return list(best.values())


def priority(job: dict, resume_words: set[str], target: set[str]) -> tuple:
    """Cheap pre-rank deciding what gets LLM scored when over the cap."""
    words = set(re.findall(r"[a-z][a-z+#.]+", (job["title"] + " " + (job["description"] or "")).lower()))
    return (job["company"].lower() in target, len(words & resume_words))


def score_new_jobs(conn, settings: dict, resume: str, resume_hash: str) -> tuple[int, int]:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE active_state != 'closed' AND (resume_hash IS NULL OR resume_hash != ?)",
        (resume_hash,))]
    resume_words = set(re.findall(r"[a-z][a-z+#.]+", resume.lower()))
    target = {c.lower() for c in settings["companies"]}
    rows.sort(key=lambda r: priority(r, resume_words, target), reverse=True)
    rows = rows[: settings["max_jobs_to_score"]]

    def score(row: dict):
        fit = llm.complete_json(llm.FIT_PROMPT.format(
            years=settings["years_experience"], resume=resume, title=row["title"], company=row["company"],
            location=row["location"], description=(row["description"] or "(no description available)")[:DESCRIPTION_LIMIT]))
        return row["id"], compute_fit(fit, resume)

    done = 0
    with ThreadPoolExecutor(SCORE_WORKERS) as pool:
        futures = [pool.submit(score, row) for row in rows]
    for future in futures:
        try:
            job_id, fit = future.result()
        except Exception:  # left unscored, so the next run retries it
            continue
        db.save_fit(conn, job_id, fit, resume_hash)
        done += 1
    return done, len(rows) - done


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9+#]+", text.lower())


def evidence_in_resume(evidence: str, resume: str) -> bool:
    """A claimed quote counts only if its words really are in the resume."""
    words = _words(evidence)
    if not words:
        return False
    present = set(_words(resume))
    return sum(w in present for w in words) / len(words) >= EVIDENCE_OVERLAP


def compute_fit(fit: dict, resume: str) -> dict:
    """The score comes from checked evidence, not from a number the model picks."""
    earned = possible = 0.0
    matched, missing = [], []
    for item in fit.get("requirements", []):
        weight = MUST_WEIGHT if item.get("importance") == "must" else NICE_WEIGHT
        status = item.get("status", "missing")
        if status != "missing" and not evidence_in_resume(item.get("resume_evidence", ""), resume):
            status = item["status"] = "missing"
        possible += weight
        earned += weight * STATUS_CREDIT.get(status, 0)
        (missing if status == "missing" else matched).append(item.get("requirement", ""))
    coverage = earned / possible if possible else 0
    score = 70 * coverage + 0.15 * fit.get("seniority_fit", 0) + 0.15 * fit.get("domain_fit", 0)
    score = max(0, min(100, round(score)))
    if fit.get("relevant_to_experience") is False:
        score = min(score, 39)
    return {**fit, "fit_score": score, "bucket": bucket_for(score), "matched": matched, "missing": missing}


def unsupported_numbers(tailored: str, resume: str) -> list[str]:
    """Numbers in the tailored resume that the original never states."""
    return sorted(set(NUMBER.findall(tailored)) - set(NUMBER.findall(resume)))


def leaked_jd_terms(tailored: str, resume: str, description: str) -> list[str]:
    """JD terms that reached the resume without being in the original: possible fabrication."""
    known = set(_words(resume))
    jd_terms = {t.lower().rstrip(".") for t in TERM.findall(description)}
    found = {t.rstrip(".") for t in TERM.findall(tailored) if t.lower().rstrip(".") in jd_terms}
    return sorted(t for t in found if not set(_words(t)) <= known)


def strip_internal_section(text: str) -> str:
    """Drop the source-only block if the model copies it in, up to the next heading."""
    kept, skipping = [], False
    for line in text.splitlines():
        if INTERNAL_HEADING in line.upper():
            skipping = True
        elif skipping and line.startswith("#"):
            skipping = False
        if not skipping:
            kept.append(line)
    return "\n".join(kept)


def tailor(conn, row: dict, resume: str) -> str:
    description = (row["description"] or "")[:DESCRIPTION_LIMIT]
    result = llm.complete_json(llm.TAILOR_PROMPT.format(
        resume=resume, title=row["title"], company=row["company"], description=description), temperature=0.3)
    text = strip_internal_section(result["resume_markdown"]).strip()
    flags = list(result.get("flags", []))
    review = []
    if invented := unsupported_numbers(text, resume):
        review.append("Numbers not in your resume: " + ", ".join(invented))
    if leaked := leaked_jd_terms(text, resume, description):
        review.append("JD terms not in your resume: " + ", ".join(leaked))

    slug = re.sub(r"\W+", "_", f"{row['company']}_{row['title']}").strip("_")[:70]
    folder = OUTPUT_DIR / "tailored"
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / f"{slug}_{row['id'][:6]}"
    markdown_to_docx(text, stem.with_suffix(".docx"))
    keywords = "\n".join(f"| {k['keyword']} | {k['status']} | {k.get('note', '')} |" for k in result.get("keywords", []))
    notes = (f"# {row['company']}: {row['title']}\n\nJD: {row['url']}\n\n"
             + ("## Check before sending\n" + "\n".join(f"- {r}" for r in review) + "\n\n" if review else "")
             + f"## Keywords\n| Keyword | Status | Note |\n|---|---|---|\n{keywords}\n\n"
             "## Changes\n" + "\n".join(f"- {c}" for c in result.get("changes", [])) +
             "\n\n## Not added: would need fabrication\n" + ("\n".join(f"- {f}" for f in flags) or "- None") + "\n")
    stem.with_name(stem.name + "_notes.md").write_text(notes)
    stem.with_suffix(".md").write_text(text + "\n")
    conn.execute("UPDATE jobs SET tailored_path = ? WHERE id = ?", (str(stem.with_suffix(".docx")), row["id"]))
    return str(stem.with_suffix(".docx"))


def tailor_pending(conn, resume: str, limit: int) -> tuple[int, int]:
    """Stretch roles are skipped here; they are tailored only on explicit request."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE bucket IN ('High', 'Medium') AND tailored_path IS NULL"
        " AND active_state != 'closed' ORDER BY fit_score DESC LIMIT ?", (limit,))]
    done = 0
    for row in rows:
        try:
            tailor(conn, row, resume)
            done += 1
        except Exception:  # left untailored, so the next run retries it
            continue
    return done, len(rows) - done


def _table(rows: list[dict]) -> str:
    lines = ["| Fit | Company | Role | Location | Open | Why | Link |", "|---|---|---|---|---|---|---|"]
    for row in rows:
        why = json.loads(row["fit_json"]).get("justification", "").replace("|", "/").replace("\n", " ")
        lines.append(f"| {row['fit_score']} | {row['company']} | {row['title']} | {row['location']} | {OPEN_LABEL[row['active_state']]} | {why} | [JD]({row['url']}) |")
    return "\n".join(lines)


def write_report(conn, analysis: dict, settings: dict, counts: dict, errors: list[str], new_ids: set[str],
                 checked: dict) -> str:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE fit_score IS NOT NULL AND status != 'dismissed' AND active_state != 'closed'"
        " ORDER BY fit_score DESC")]
    open_count = sum(r["active_state"] == "active" for r in rows)
    out = [f"# Job run {datetime.now():%Y-%m-%d %H:%M}", "",
           f"Open-role check: {open_count} confirmed open, {len(rows) - open_count} unconfirmed, "
           f"{checked['closed']} closed roles removed this run.", ""]
    out += ["## Target roles", "", "| Role | Why |", "|---|---|"]
    out += [f"| {r['title']} | {r['reasoning']} |" for r in analysis.get("target_roles", [])]
    out += ["", "## Core strengths", ""] + [f"- {s['strength']}: {s['evidence']}" for s in analysis.get("core_strengths", [])]
    out += ["", "## Gaps", ""] + [f"- {g['gap']}: {g['why_it_matters']}" for g in analysis.get("gaps", [])]
    for bucket, label in (("High", "High-probability (80-100)"), ("Medium", "Medium-probability (60-79)"),
                          ("Stretch", "Stretch (40-59)")):
        matching = [r for r in rows if r["bucket"] == bucket]
        fresh = sum(r["id"] in new_ids for r in matching)
        out += ["", f"## {label}: {len(matching)} roles, {fresh} new", ""]
        if matching:
            out.append(_table(matching))
    excluded = sum(r["bucket"] == "Excluded" for r in rows)
    out += ["", f"Excluded below 40: {excluded} roles.", "", "## Companies with no relevant openings", ""]
    bucketed = {r["company"].lower() for r in rows if r["bucket"] != "Excluded"}
    pending = {r[0].lower() for r in conn.execute(
        "SELECT company FROM jobs WHERE fit_score IS NULL AND active_state != 'closed'")}
    waiting = len(conn.execute(
        "SELECT id FROM jobs WHERE fit_score IS NULL AND active_state != 'closed'").fetchall())
    not_searched = []
    for company in settings["companies"]:
        if counts.get(company) is None:
            not_searched.append(company)
        elif company.lower() not in bucketed | pending:
            out.append(f"- {company}: no relevant openings in {', '.join(settings['locations'])}.")
    if waiting:
        out += ["", f"{waiting} more roles are waiting to be scored and carry over to the next run."]
    if not_searched:
        out += ["", "Not searched this run: " + ", ".join(not_searched) + "."]
    out += ["", "## Open positions by company", ""]
    for company, openings in company_openings(conn, settings["companies"]).items():
        coverage = "career site" if company in FEEDS else "LinkedIn/Naukri only"
        out += [f"### {company}: {len(openings)} open ({coverage})", ""]
        for row in openings:
            fit = "not scored yet" if row["fit_score"] is None else f"fit {row['fit_score']}"
            out.append(f"- [{row['title']}]({row['url']}), {row['location']}, {fit}, via {row['source']}")
        out.append("")
    if errors:
        out += ["## Source problems", ""] + [f"- {e}" for e in errors]
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"report_{datetime.now():%Y-%m-%d_%H%M}.md"
    path.write_text("\n".join(out) + "\n")
    return str(path)


def run() -> dict:
    with run_lock():
        return _run()


def _run() -> dict:
    settings = load_settings()
    resume, resume_hash = load_resume()
    with db.connect() as conn:
        cursor = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (db.now(),))
        run_id = cursor.lastrowid
        conn.commit()
        analysis = get_analysis(conn, resume, resume_hash)
        conn.commit()
        roles = [r["title"] for r in analysis.get("target_roles", [])]
        run_start = db.now()
        found, counts, errors = collect(settings, roles)
        kept = filter_jobs(found, settings)
        new_ids = {job.id for job in kept if db.upsert_job(conn, job)}
        conn.commit()
        checked = liveness.verify_jobs(conn, run_start)
        conn.commit()
        scored, unscored = score_new_jobs(conn, settings, resume, resume_hash)
        conn.commit()
        if unscored:
            errors.append(f"{unscored} roles could not be scored and will retry next run. Check OpenAI credits.")
        tailored, failed = tailor_pending(conn, resume, settings["max_tailored_per_run"])
        if failed:
            errors.append(f"{failed} tailored resumes failed and will retry next run")
        emails = 0
        if inbox.configured():
            try:
                emails = inbox.scan(conn, settings["companies"])["emails"]
            except Exception as exc:
                errors.append(f"Email check failed: {describe_error(exc)}")
        report = write_report(conn, analysis, settings, counts, errors, new_ids, checked)
        summary = {"found": len(found), "kept": len(kept), "new": len(new_ids), "closed": checked["closed"],
                   "scored": scored, "tailored": tailored, "emails": emails, "errors": errors}
        conn.execute("UPDATE runs SET finished_at = ?, summary_json = ?, report_path = ? WHERE id = ?",
                     (db.now(), json.dumps(summary), report, run_id))
    return {**summary, "report": report}
