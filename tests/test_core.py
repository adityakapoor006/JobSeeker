import html
import json
import sqlite3

from jobseeker.config import DEFAULT_SETTINGS, location_terms
from jobseeker.db import SCHEMA
from jobseeker.liveness import check_deloitte, check_hsbc, check_job, check_sitemap
from jobseeker.pipeline import (bucket_for, company_openings, compute_fit, evidence_in_resume, filter_jobs,
                                leaked_jd_terms, unsupported_numbers)
from jobseeker import pipeline
from jobseeker.sources.apify import foundit_item_to_job, indeed_item_to_job, linkedin_item_to_job, naukri_item_to_job
from jobseeker.sources.base import Job, experience_range, fits_experience, in_locations, relevant_title
from jobseeker.sources.direct import (goldman_roles, juspay_openings, parse_job_page, sitemap_job_urls, usi_cards,
                                      usi_locations)


def test_buckets_match_spec():
    assert [bucket_for(s) for s in (100, 80, 79, 60, 59, 40, 39, 0)] == [
        "High", "High", "Medium", "Medium", "Stretch", "Stretch", "Excluded", "Excluded"]


def test_city_aliases_cover_defaults():
    terms = location_terms(DEFAULT_SETTINGS)
    assert in_locations("Bangalore, IN", terms, True)
    assert in_locations("Gurgaon, Haryana", terms, True)
    assert in_locations("Pune Maharashtra India", terms, True)
    assert not in_locations("Hyderabad, India", terms, True)
    assert in_locations("Remote - India", terms, True)
    assert not in_locations("Remote - India", terms, False)
    assert not in_locations("Remote - USA", terms, True)


def test_title_filter():
    assert relevant_title("Senior Software Engineer")
    assert relevant_title("Staff AI Engineer")
    assert not relevant_title("Software Engineer Intern")
    assert not relevant_title("Account Executive")


def test_experience():
    assert experience_range("5-8 years of Java") == (5, 8)
    assert experience_range("minimum 8+ years of experience") == (8, None)
    assert experience_range("a 25 years old company") == (None, None)
    assert fits_experience(Job("a", "t", "l", "u", "s", exp_min=5, exp_max=8), 8)
    assert not fits_experience(Job("a", "t", "l", "u", "s", exp_min=0, exp_max=2), 8)
    assert not fits_experience(Job("a", "t", "l", "u", "s", exp_min=15), 8)
    assert fits_experience(Job("a", "t", "l", "u", "s"), 8)


def test_careers_copy_wins_over_board_copy():
    board = Job("Stripe", "Senior Engineer", "Bengaluru", "https://l/1", "linkedin")
    site = Job("Stripe", "Senior Engineer", "Bengaluru", "https://c/1", "careers")
    assert filter_jobs([board, site], DEFAULT_SETTINGS) == [site]


def test_unsupported_numbers_catches_invented_metric():
    assert unsupported_numbers("improved 45% over 8 years", "8+ years, 30% efficiency") == ["45"]


def test_apify_mappings_use_real_field_shapes():
    job = linkedin_item_to_job({"title": "Senior Software Engineer", "companyName": "Scapia",
                                "location": "Bengaluru, Karnataka, India", "applyUrl": "",
                                "url": "https://in.linkedin.com/jobs/view/x-1?a=b",
                                "postedDate": "2026-10-29T00:00:00.000Z", "description": "d"})
    assert job.url == "https://in.linkedin.com/jobs/view/x-1" and job.posted_at == "2026-10-29"
    job = naukri_item_to_job({"title": "Senior Software Engineer", "url": "https://naukri/1", "consultant": False,
                              "company": {"name": "Epam Systems"}, "experience": {"minimum": "5", "maximum": "8"},
                              "createdDate": 1793491294248, "locations": [{"label": "Bengaluru"}],
                              "description": {"full": "<p>Java</p>"}, "skills": {"other": ["Kafka"]}})
    assert (job.exp_min, job.exp_max) == (5, 8) and "Kafka" in job.description
    assert naukri_item_to_job({"title": "Senior Engineer", "url": "u", "consultant": True}) is None


def test_indeed_and_foundit_mappings_use_real_field_shapes():
    item = {"url": "https://in.indeed.com/viewjob?jk=3a57", "title": "Full Stack Senior Software Engineer",
            "expired": False, "datePublished": "2026-10-27T05:00:00.000Z", "employer": {"name": "Beckman Coulter"},
            "location": {"countryName": "India", "admin3Code": "BLR", "city": ""}, "description": {"text": "Java"}}
    job = indeed_item_to_job(item, "Bengaluru")
    assert (job.location, job.posted_at, job.description) == ("Bengaluru, India", "2026-10-27", "Java")
    assert indeed_item_to_job({**item, "expired": True}, "Bengaluru") is None
    item = {"job_id": "69678857", "title": "Senior Software Engineer, PDX", "company": "Intuit",
            "location": "Bengaluru, India", "experience_min_years": 7, "experience_max_years": 9,
            "skills": ["Java", "React"], "functions": ["Software Engineering"], "posted_days_ago": 1,
            "apply_url": "https://www.foundit.in/job/x-69678857", "scraped_at": "2026-11-02T15:41:30.986Z"}
    job = foundit_item_to_job(item)
    assert (job.exp_min, job.exp_max, job.posted_at) == (7, 9, "2026-11-01") and "Java, React" in job.description
    assert foundit_item_to_job({"_type": "summary", "jobs_scraped": 3}) is None


def test_second_run_is_refused_while_one_is_in_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "LOCK_PATH", tmp_path / "run.lock")
    monkeypatch.setattr(pipeline, "DATA_DIR", tmp_path)
    with pipeline.run_lock():
        try:
            with pipeline.run_lock():
                raise AssertionError("second run should be refused")
        except pipeline.RunInProgress:
            pass
    with pipeline.run_lock():  # free again once the first run ends
        pass


def test_job_id_keeps_query_string():
    assert Job("a", "t", "l", "https://x/jobs?gh_jid=1", "s").id != Job("a", "t", "l", "https://x/jobs?gh_jid=2", "s").id


RESUME = "Built microservices using Java 17 and Spring Boot with Kafka"


def test_evidence_must_exist_in_resume():
    assert evidence_in_resume("microservices using Java 17", RESUME)
    assert not evidence_in_resume("five years of Kubernetes on GCP", RESUME)


def test_fit_score_is_computed_and_unproven_claims_are_missing():
    fit = {"requirements": [
        {"requirement": "Java", "importance": "must", "status": "met", "resume_evidence": "Java 17"},
        {"requirement": "GCP", "importance": "must", "status": "met", "resume_evidence": "deep GCP expertise"},
    ], "seniority_fit": 100, "domain_fit": 100, "relevant_to_experience": True}
    result = compute_fit(fit, RESUME)
    assert result["fit_score"] == 65 and result["bucket"] == "Medium"
    assert result["missing"] == ["GCP"] and result["matched"] == ["Java"]


def test_wrong_level_is_excluded():
    fit = {"requirements": [{"requirement": "Java", "importance": "must", "status": "met", "resume_evidence": "Java 17"}],
           "seniority_fit": 90, "domain_fit": 90, "relevant_to_experience": False}
    assert compute_fit(fit, RESUME)["bucket"] == "Excluded"


def test_leaked_jd_terms_flags_unsupported_keyword():
    assert leaked_jd_terms("Used Java and Cucumber daily", RESUME, "Needs Java, Cucumber") == ["Cucumber"]


def test_only_explicit_signals_close_a_role():
    from jobseeker.liveness import ACTIVE, CLOSED, UNVERIFIED, from_status
    assert from_status(200) == ACTIVE
    assert from_status(404) == CLOSED and from_status(410) == CLOSED
    assert from_status(429) == UNVERIFIED and from_status(503) == UNVERIFIED


def test_failed_check_is_not_treated_as_closed():
    import httpx
    from jobseeker.liveness import UNVERIFIED, check_job

    def fail(request):
        raise httpx.ConnectError("down")

    client = httpx.Client(transport=httpx.MockTransport(fail))
    row = {"company": "Google", "source": "careers", "url": "https://careers.google.com/jobs/results/1/"}
    assert check_job(row, client, None) == UNVERIFIED
    assert check_job({**row, "company": "Atlassian"}, client, None) == UNVERIFIED


def test_board_role_rate_limited_stays_unconfirmed():
    import httpx
    from jobseeker.liveness import CLOSED, UNVERIFIED, check_board

    limited = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    closed = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="No longer accepting applications")))
    assert check_board({"url": "https://x/1"}, limited) == UNVERIFIED
    assert check_board({"url": "https://x/1"}, closed) == CLOSED


def test_internal_section_is_removed_but_resume_is_kept():
    from jobseeker.pipeline import strip_internal_section
    text = "## SKILLS\n- Java\n## ADDITIONAL VERIFIED DETAIL FROM EARLIER RESUME VERSIONS\n- note\n## EDUCATION\n- B.Tech"
    assert strip_internal_section(text) == "## SKILLS\n- Java\n## EDUCATION\n- B.Tech"


def test_sitemap_urls_are_jobs_only_and_newest_first():
    xml = ("<loc>https://x.com/job/Pune-Old-Engineer/100/</loc><loc>https://x.com/about</loc>"
           "<loc>https://x.com/job/Pune-New-&amp;-Engineer/200/</loc>")
    assert sitemap_job_urls(xml) == ["https://x.com/job/Pune-New-&-Engineer/200/", "https://x.com/job/Pune-Old-Engineer/100/"]


def test_job_page_title_location_and_text():
    terms = location_terms(DEFAULT_SETTINGS)
    page = '<title>Data Engineer (Dbt) Job Details | Nestle SA</title><nav>menu</nav><div class="jobDisplay">Build pipes</div>'
    job = parse_job_page("Nestle", "https://x.com/job/Bangalore-Data-Engineer-(Dbt)/1/", page, terms)
    assert (job.title, job.location) == ("Data Engineer (Dbt)", "Bangalore, India")
    assert "menu" not in job.description and "Build pipes" in job.description
    page = ('<title>Senior Data Engineer - Ag &amp; Trading at Cargill</title>'
            '<script type="application/ld+json">{"description": "<p>Trade data</p>"}</script>')
    job = parse_job_page("Cargill", "https://x.com/en/job/gurgaon/senior-data-engineer/23251/1", page, terms)
    assert (job.title, job.location, job.description) == ("Senior Data Engineer - Ag & Trading", "Gurgaon, India", "Trade data")


def test_juspay_openings_skip_closed_postings():
    props = json.dumps({"jobData": [1, [[0, {"job_id": [0, "A1"], "opening_status": [0, True]}],
                                        [0, {"job_id": [0, "B2"], "opening_status": [0, False]}]]]})
    page = f'<astro-island component-url="/_astro/JobListings.x.js" props="{html.escape(props)}">'
    assert [job["job_id"] for job in juspay_openings(page)] == ["A1"]


def test_redirect_away_from_job_page_means_closed():
    class Client:
        def __init__(self, url): self.final = url
        def get(self, url): return type("R", (), {"status_code": 200, "url": self.final})()
    row = {"url": "https://x.com/job/Pune-Engineer/1/"}
    assert check_sitemap(row, {}, Client("https://x.com/job/Pune-Engineer/1/?locale=en_GB")) == "active"
    assert check_sitemap(row, {}, Client("https://x.com/errorpage/?errortype=Exception")) == "closed"
    assert check_sitemap(row, {}, Client("https://x.com/en")) == "closed"


def test_title_filter_keeps_spelled_out_artificial_intelligence():
    assert relevant_title("Assistant Director - Automation and Artificial Intelligence")


def test_hsbc_check_reads_the_status_of_the_posting():
    class Client:
        def __init__(self, status): self.status = status
        def get(self, url, params): return type("R", (), {"status_code": self.status})()
    row = {"url": "https://portal.careers.hsbc.com/careers/job/563"}
    assert check_hsbc(row, {}, Client(200)) == "active"
    assert check_hsbc(row, {}, Client(404)) == "closed"
    assert check_hsbc(row, {}, Client(503)) == "unverified"


def test_goldman_listing_keeps_posted_roles_and_rejects_an_empty_page():
    class Client:
        def __init__(self, items): self.items = items
        def post(self, url, json, headers):
            data = {"data": {"roleSearch": {"items": self.items}}}
            return type("R", (), {"json": lambda self: data})()
    roles = goldman_roles(Client([{"status": "POSTED"}, {"status": "CLOSED"}]))
    assert roles == [{"status": "POSTED"}]
    try:
        goldman_roles(Client([]))
    except ValueError:
        return
    raise AssertionError("an empty listing must fail, or every role would look closed")


def test_goldman_role_closes_only_when_the_listing_loaded_without_it():
    row = {"company": "Goldman Sachs", "source": "careers", "url": "https://higher.gs.com/roles/1"}
    assert check_job(row, None, None, {"https://higher.gs.com/roles/1"}) == "active"
    assert check_job(row, None, None, {"https://higher.gs.com/roles/2"}) == "closed"
    assert check_job(row, None, None, None) == "unverified"


def test_usi_cards_and_full_location_list():
    page = ('<h3 class="article__header__text__title x"> <a href="https://u/JobDetail/A/1" class="link"> AI Engineer &amp; Lead </a>'
            ' </h3> <div class="article__header__text__subtitle"> <span> Deloitte </span> | <span>Multiple Locations</span> </div>')
    assert usi_cards(page) == [("https://u/JobDetail/A/1", "AI Engineer & Lead", "Multiple Locations")]
    detail = ('<div class="article__header--locations display--none"> <div class="fluid-cols"> '
              '<p class="paragraph">Bengaluru, Karnataka, India</p> <p class="paragraph">Hyderabad, Telangana, India</p> </div> </div>')
    assert usi_locations(detail, "Multiple Locations") == "Bengaluru, Karnataka, India; Hyderabad, Telangana, India"
    assert usi_locations("<p>no block</p>", "Pune, India") == "Pune, India"


def test_usi_role_closes_on_error_page_only():
    class Client:
        def __init__(self, url, status, text=""): self.response = type("R", (), {"url": url, "status_code": status, "text": text})()
        def get(self, url): return self.response
    row = {"url": "https://usijobs.deloitte.com/en_US/careersUSI/JobDetail/X/1"}
    assert check_deloitte(row, {}, Client(row["url"], 200, '{"@type": "JobPosting"}')) == "active"
    assert check_deloitte(row, {}, Client("https://usijobs.deloitte.com/en_US/careersUSI/Error", 403)) == "closed"
    assert check_deloitte(row, {}, Client(row["url"], 503)) == "unverified"


def test_company_openings_match_board_names_and_include_unscored():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    rows = [("a", "Walmart Global Tech India", 90, "active", "new"), ("b", "Walmart", None, "active", "new"),
            ("c", "Walmart", 95, "closed", "new"), ("d", "Walmart", 95, "active", "dismissed"),
            ("e", "ANZ", 70, "active", "new")]
    for job_id, company, fit, state, status in rows:
        conn.execute("INSERT INTO jobs (id, company, title, url, source, first_seen, last_seen, fit_score, active_state, status)"
                     " VALUES (?,?,'Engineer','u','careers','t','t',?,?,?)", (job_id, company, fit, state, status))
    found = company_openings(conn, ["Walmart", "ANZ Bank", "Juspay"])
    assert [r["id"] for r in found["Walmart"]] == ["a", "b"]
    assert [r["id"] for r in found["ANZ Bank"]] == ["e"]
    assert found["Juspay"] == []


def _email(sender, subject, body, html_body=False, date="Mon, 02 Nov 2026 10:00:00 +0530"):
    from email.message import EmailMessage
    message = EmailMessage()
    message["From"], message["Subject"], message["Date"] = sender, subject, date
    message["Message-ID"] = f"<{abs(hash(subject + date))}@test>"
    message.set_content(body, subtype="html" if html_body else "plain")
    return message


def test_email_classification_rules():
    from jobseeker.inbox import classify
    assert classify("Your application to HSBC", "Unfortunately we will not be progressing your application") == "Rejection"
    assert classify("Interview invitation: Data Engineer", "") == "Interview"
    assert classify("Thank you for applying", "If shortlisted we will contact you for an interview") == \
        "Application received"
    assert classify("Complete your HackerRank test", "") == "Assessment"
    assert classify("Lunch on Friday?", "See you there") is None


def test_email_filter_ignores_shopping_offers():
    from jobseeker.inbox import looks_job_related
    assert looks_job_related("Workday <noreply@hsbc.myworkday.com>", "Hello")
    assert looks_job_related("Priya <priya@acme.com>", "Senior Data Engineer opportunity")
    assert not looks_job_related("Shop <deals@store.com>", "50% off offer ends tonight")


def test_email_updates_matched_role():
    from jobseeker.inbox import record
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO jobs (id, company, title, url, source, first_seen, last_seen) VALUES "
                 "('j1', 'Goldman Sachs', 'Data Engineer', 'u', 'careers', 'x', 'x')")
    messages = [
        _email("Goldman Sachs Careers <careers@gs.com>", "Thank you for your application",
               "<p>We received your application for <b>Data Engineer</b>.</p>", html_body=True),
        _email("Goldman Sachs Careers <careers@gs.com>", "Interview invitation",
               "We would like to invite you to an interview for the Data Engineer role.",
               date="Tue, 03 Nov 2026 10:00:00 +0530"),
        _email("Friend <a@b.com>", "Weekend plans", "Unfortunately I can't come."),
    ]
    stats = record(conn, messages, ["Goldman Sachs"], use_llm=False)
    assert stats["emails"] == 2 and stats["updated_roles"] == 2
    rows = [dict(r) for r in conn.execute("SELECT category, company, job_id FROM emails ORDER BY received_at")]
    assert [r["category"] for r in rows] == ["Application received", "Interview"]
    assert all(r["company"] == "Goldman Sachs" and r["job_id"] == "j1" for r in rows)
    assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "interviewing"
    assert record(conn, messages, ["Goldman Sachs"], use_llm=False)["emails"] == 0


def test_email_rules_skip_newsletters_and_alerts():
    from jobseeker.inbox import ALERT_SENDER, classify
    assert classify("Caution: Beware of fake and lucrative job offers!", "") is None
    assert classify("IndusInd Platinum Card assessment complete", "") is None
    assert classify("6 things to know on World Polio Day", "Unfortunately polio still exists") is None
    assert classify("Your application", "Unfortunately, we have decided to move forward with other candidates") \
        == "Rejection"
    assert classify("Next steps", "Please complete the online assessment by Friday") == "Assessment"
    assert ALERT_SENDER.search("jobalerts-noreply@linkedin.com") and ALERT_SENDER.search("noreply@unstop.news")
    assert not ALERT_SENDER.search("careers@stripe.com")
