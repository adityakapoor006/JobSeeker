"""OpenAI JSON calls for resume analysis, fit scoring and tailoring."""
import json

from openai import OpenAI

from .config import env

TRUTH_RULES = """Rules you must never break:
- Use only facts present in the RESUME. Never invent skills, employers, dates, metrics, titles or claims.
- Do not add a number the resume does not state.
- If a JD keyword cannot be supported by the resume, do not add it: report it as a gap to flag."""

ANALYSIS_PROMPT = """You are a senior technical recruiter. Analyse the RESUME and return JSON:
{"years_experience": int,
 "target_roles": [{"title": str, "reasoning": str}],
 "core_strengths": [{"strength": str, "evidence": str}],
 "gaps": [{"gap": str, "why_it_matters": str}]}
target_roles: exactly 5 market-standard job titles, each short enough to use as a job-board search.
Base them on the job titles held and what most experience bullets describe, not on the summary or
competency lists. Match the candidate's seniority. A title belongs only if its main daily work is what
this person has actually been doing. Never suggest a generic or invented title.
reasoning and evidence must cite specific resume content.
core_strengths: 4-6 true differentiators, not generic skills.
gaps: 4-6 skills or keywords that competing candidates for THESE target roles commonly have and this
resume lacks. Name concrete skills, for example a specific cloud platform or tool, not broad areas.
Before listing a gap, check the resume does not already cover it. Ignore skills unrelated to these roles.

RESUME:
"""

FIT_PROMPT = """You are a strict technical recruiter comparing one job with one candidate
who has about {years} years of experience. Use only actual resume content, not assumptions.

List the job's 5-10 real requirements. For each give:
- "requirement": short text
- "importance": "must" or "nice"
- "status": "met", "partial" or "missing"
- "resume_evidence": a phrase copied verbatim from the resume, or "" when missing
Count a related technology as "partial", never "met".
Also give "seniority_fit" 0-100 (how well the job's level matches the candidate) and
"domain_fit" 0-100 (how close the job's domain is to the resume's domains).
Set "relevant_to_experience" false if the role is far above or below this level.

Return JSON: {{"requirements": [...], "seniority_fit": int, "domain_fit": int,
"relevant_to_experience": bool, "justification": str, "concerns": str}}
"justification" is at most 2 lines and names resume content.
If the description is too thin to judge, say so in "concerns".

RESUME:
{resume}

JOB: {title} at {company} ({location})
DESCRIPTION:
{description}
"""

TAILOR_PROMPT = """You are an expert resume writer tailoring a resume to one job for ATS and recruiters.
""" + TRUTH_RULES + """
The RESUME may end with a block of additional verified detail. Those facts are true and usable, for
example inside PROJECTS, but that heading must never appear in your output.

Tasks:
1. Find the top 5-8 JD keywords/skills missing or under-emphasised in the resume.
   Read the resume closely first: Kafka supports event-driven work, HTML supports HTML5.
   Status "added": now stated, backed by a resume fact. "emphasised": already there, moved up or
   strengthened. "flagged_gap": unsupported, so left out.
2. Really rewrite the bullets most relevant to this JD: use the JD's terminology wherever the same
   fact supports it, and put existing quantified impact first. Rewrite at least 5 bullets. Do not
   copy them unchanged, and do not change what actually happened.
3. Reorder bullets within each job so the most relevant come first. Rewrite SUMMARY for this role.
   Reorder SKILLS so the JD's priorities lead. Move sections if the JD prioritises something buried.
4. Put any keyword needing fabrication in "flags", never in the resume.
Keep every employer, title, date, degree and contact detail exactly as written.
"changes" must describe only edits you really made.

Format resume_markdown: "# Name", then a contact line, then "## SECTION" headings, "### Title, Employer  dates"
for jobs, and "- " bullets. Nothing else: no notes, no commentary.

Return JSON: {{"keywords": [{{"keyword": str, "status": "added" | "emphasised" | "flagged_gap", "note": str}}],
"changes": [str], "flags": [str], "resume_markdown": str}}

RESUME:
{resume}

JOB: {title} at {company}
DESCRIPTION:
{description}
"""


def complete_json(prompt: str, temperature: float = 0.1) -> dict:
    client = OpenAI(api_key=env("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model=env("OPENAI_MODEL") or "gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=temperature,
    )
    return json.loads(response.choices[0].message.content)
