"""Dashboard: streamlit run app.py"""
import json
from pathlib import Path

import pandas as pd
import streamlit as st

from jobseeker import db, liveness, pipeline, schedule
from jobseeker.config import FEEDS, RESUME_DIR, SETTINGS_PATH, env, load_settings, save_settings
from jobseeker.resume import latest_resume, load_resume

st.set_page_config(page_title="JobSeeker", layout="wide")
st.title("JobSeeker")
settings = load_settings()


def save_location_form(key: str) -> None:
    locations = st.text_input("Cities, comma separated", ", ".join(settings["locations"]), key=f"{key}_loc")
    remote = st.checkbox("Include remote roles open in India", settings["include_remote_india"], key=f"{key}_rem")
    if st.button("Save locations", key=f"{key}_save"):
        settings["locations"] = [c.strip() for c in locations.split(",") if c.strip()]
        settings["include_remote_india"] = remote
        save_settings(settings)
        st.rerun()


if not SETTINGS_PATH.exists():
    st.subheader("Where do you want to work?")
    save_location_form("first")
    st.stop()

with db.connect() as conn:
    jobs = [dict(r) for r in conn.execute("SELECT * FROM jobs WHERE fit_score IS NOT NULL AND active_state != 'closed' ORDER BY fit_score DESC")]
    last = conn.execute("SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    analysis = conn.execute("SELECT analysis_json FROM analysis ORDER BY created_at DESC LIMIT 1").fetchone()

top = st.columns([3, 1])
if last:
    summary = json.loads(last["summary_json"])
    top[0].caption(f"Last run {last['finished_at']}: {summary['new']} new, {summary['scored']} scored, "
                   f"{summary['tailored']} tailored")
    for error in summary["errors"]:
        top[0].warning(error)
if not env("APIFY_TOKEN"):
    top[0].info("Add APIFY_TOKEN to .env to include LinkedIn, Naukri, Indeed and Foundit.")
scheduled = schedule.is_enabled()
wanted = top[1].toggle("Scheduled runs (9 am, 9 pm)", scheduled,
                       help="Each run spends Apify credit and, once scoring works, OpenAI credit.")
if wanted != scheduled:
    schedule.enable() if wanted else schedule.disable()
    st.rerun()
if top[1].button("Run now", type="primary"):
    try:
        with st.spinner("Searching, scoring and tailoring. This can take several minutes."):
            pipeline.run()
        st.rerun()
    except pipeline.RunInProgress as exc:
        top[0].warning(str(exc))


def role_table(bucket: str) -> None:
    rows = [j for j in jobs if j["bucket"] == bucket and j["status"] != "dismissed"]
    if not rows:
        st.write("No roles in this bucket.")
        return
    frame = pd.DataFrame([{
        "Fit": j["fit_score"], "Company": j["company"], "Role": j["title"], "Location": j["location"],
        "Open": "confirmed" if j["active_state"] == "active" else "unconfirmed",
        "Checked": (j["verified_at"] or "")[:16].replace("T", " "),
        "Source": j["source"], "Status": j["status"], "JD": j["url"],
        "Why": json.loads(j["fit_json"]).get("justification", ""),
    } for j in rows])
    st.dataframe(frame, hide_index=True, width="stretch",
                 column_config={"JD": st.column_config.LinkColumn("JD", display_text="Open")})
    labels = {f"{j['fit_score']} | {j['company']} | {j['title']}": j for j in rows}
    choice = st.selectbox("Role details", list(labels), key=f"pick_{bucket}")
    job = labels[choice]
    fit = json.loads(job["fit_json"])
    left, right = st.columns(2)
    left.markdown("**Matched**\n" + "\n".join(f"- {m}" for m in fit.get("matched", [])))
    right.markdown("**Missing**\n" + "\n".join(f"- {m}" for m in fit.get("missing", [])))
    if fit.get("concerns"):
        st.caption(f"Concerns: {fit['concerns']}")
    buttons = st.columns(4)
    if job["tailored_path"]:
        path = Path(job["tailored_path"])
        buttons[0].download_button("Download tailored resume", path.read_bytes(), path.name, key=f"dl_{job['id']}")
        notes = path.with_name(path.stem + "_notes.md")
        if notes.exists():
            with st.expander("Tailoring notes and flags"):
                st.markdown(notes.read_text())
    elif bucket == "Stretch" and buttons[0].button("Tailor this role", key=f"t_{job['id']}"):
        with st.spinner("Tailoring"), db.connect() as conn:
            pipeline.tailor(conn, job, load_resume()[0])
        st.rerun()
    for column, (label, status) in zip(buttons[1:], (("Mark applied", "applied"), ("Dismiss", "dismissed"))):
        if column.button(label, key=f"{status}_{job['id']}"):
            with db.connect() as conn:
                conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job["id"]))
            st.rerun()


if top[1].button("Check roles are open"):
    with st.spinner("Checking every role against its live posting"), db.connect() as conn:
        result = liveness.verify_jobs(conn)
    st.success(f"{result['active']} open, {result['closed']} closed and removed, {result['unverified']} unconfirmed")

tabs = st.tabs(["Open positions", "High (80-100)", "Medium (60-79)", "Stretch (40-59)", "Resume analysis", "Settings"])
for tab, bucket in zip(tabs[1:], ("High", "Medium", "Stretch")):
    with tab:
        role_table(bucket)
tabs[3].caption(f"Excluded below 40: {sum(j['bucket'] == 'Excluded' for j in jobs)} roles.")

with tabs[0]:
    with db.connect() as conn:
        openings = pipeline.company_openings(conn, settings["companies"])
    career_only = st.checkbox("Company career sites only", True)
    if career_only:
        openings = {c: [r for r in rows if r["source"] == "careers"] for c, rows in openings.items()}
    st.dataframe(pd.DataFrame([{
        "Company": company, "Open roles": len(rows),
        "Searched through": "Career site" if company in FEEDS else "LinkedIn/Naukri only",
    } for company, rows in openings.items()]), hide_index=True, width="stretch")
    picked = st.selectbox("Show roles for", ["All companies"] + list(openings))
    chosen = [r for c, rows in openings.items() if picked in ("All companies", c) for r in rows]
    if chosen:
        st.dataframe(pd.DataFrame([{
            "Fit": j["fit_score"], "Company": j["company"], "Role": j["title"], "Location": j["location"],
            "Open": "confirmed" if j["active_state"] == "active" else "unconfirmed",
            "Source": j["source"], "JD": j["url"],
        } for j in chosen]), hide_index=True, width="stretch",
            column_config={"JD": st.column_config.LinkColumn("JD", display_text="Open")})
    else:
        st.write("No open roles matching your cities and experience.")

with tabs[4]:
    st.caption(f"Resume in use: {latest_resume().name}")
    upload = st.file_uploader("Replace resume (analysis reruns on the next run)", ["docx", "pdf", "txt", "md"])
    if upload and st.button("Use this resume"):
        (RESUME_DIR / upload.name).write_bytes(upload.getvalue())
        st.rerun()
    if analysis:
        data = json.loads(analysis["analysis_json"])
        st.subheader("Top 5 target roles")
        st.table(pd.DataFrame(data.get("target_roles", [])))
        st.subheader("Core strengths")
        st.table(pd.DataFrame(data.get("core_strengths", [])))
        st.subheader("Gaps")
        st.table(pd.DataFrame(data.get("gaps", [])))

with tabs[5]:
    st.subheader("Locations")
    save_location_form("settings")
    st.subheader("Experience")
    years = st.number_input("Years of experience", 0, 40, settings["years_experience"])
    if years != settings["years_experience"]:
        settings["years_experience"] = int(years)
        save_settings(settings)
    st.subheader("Target companies")
    new_company = st.text_input("Add a company")
    if st.button("Add") and new_company.strip():
        settings["companies"].append(new_company.strip())
        save_settings(settings)
        st.rerun()
    kept = st.multiselect("Current list", settings["companies"], settings["companies"])
    if kept != settings["companies"]:
        settings["companies"] = kept
        save_settings(settings)
        st.rerun()
    st.caption("Direct careers feed: " + ", ".join(c for c in settings["companies"] if c in FEEDS))
    st.caption("LinkedIn/Naukri only: " + ", ".join(c for c in settings["companies"] if c not in FEEDS))
