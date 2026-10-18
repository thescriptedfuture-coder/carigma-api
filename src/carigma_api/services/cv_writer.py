"""CV Architect: one job, one tailored CV, nothing the person did not tell us.

V2 could show a tailored CV and download it in six layouts — but only the nine
V1 had built. Every job a V2 scan found ended at "No apply kit has been built
for this job … Nothing in this build creates one." This is what builds one.

## What it is

V1's `generate_cv_data`, ported: one call, reading the posting and the profile,
returning the CV the templates already render, plus two things the job page
already has a place for — the gaps (what the posting asks that the profile
cannot support) and an outreach note. 15 credits (`cv_generate`), V1's price.

## What it may not do, checked rather than requested

V1's hard rules were all in the prompt: "NEVER fabricate. No invented
employers, job titles, dates, degrees, certifications, tools, clients, or
numbers." The prompt still says so. The output is then held to it against the
profile text the model was given (`grounding`):

- **Contact details are ours, not the model's.** Name, email, location and
  LinkedIn come from the account and the profile. The phone is left out: V1
  printed "+91 XXXXX XXXXX", a blank on a document sent to an employer.
- **An employer the profile does not name is dropped**, with its role. At
  least half its words must be the profile's — decoration like "Ministry of
  Defence" survives, a different employer does not.
- **A core skill the profile does not contain is dropped** (every word), and a
  certification likewise (most of its words: "Professional Certificate" gets
  appended to real ones).
- **A bullet, achievement or impact line citing a figure the profile does not
  contain is dropped.** A summary or tagline doing so fails the whole CV —
  those are the first lines an employer reads.

When what survives is not a CV (no summary, no experience), the run is EMPTY,
and an empty run is never charged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from carigma_api.services import grounding as rules

logger = logging.getLogger(__name__)

#: `(system, user, max_tokens) -> parsed JSON`. The real one is `call_claude_json`.
Writer = Callable[[str, str, int], Any]

MAX_TOKENS = 3500

#: How much of a posting is read. V1 read 2,500 characters of it.
DESCRIPTION_CHARS = 4000

SYSTEM = (
    "You are Carigma's CV Architect. You craft truthful, ATS-optimised CVs that get "
    "callbacks. Return ONLY a raw JSON object.\n\n"
    "HARD RULES:\n"
    "1. NEVER fabricate. No invented employers, job titles, dates, degrees, "
    "certifications, tools, clients or numbers. Every fact comes from the candidate "
    "data provided.\n"
    "2. Metrics: a number appears ONLY if it is in the candidate's data. A real "
    "bullet without a number beats a fake percentage.\n"
    "3. RELEVANCE: this CV targets ONE role. Lead with what matters for it; compress "
    "or drop what does not. Never pad with skills the candidate did not list.\n"
    "4. Use the posting's own words for a requirement ONLY where the candidate's "
    "background genuinely supports it. A requirement they cannot support is a gap — "
    "list it under gaps, never write it into the CV."
)


class NothingToTailorFrom(ValueError):
    """The profile has no experience to tailor. Not charged, not attempted."""


@dataclass(frozen=True)
class Candidate:
    """What the CV may be built from, and the evidence its claims are held to."""

    name: str
    email: str
    location: str
    linkedin: str
    current_role: str
    experience: str
    skills: str
    education: str
    certifications: str
    achievement: str
    #: Every word of the profile, for `grounding.supported`.
    corpus: frozenset[str]
    #: Every figure in the profile, for `grounding.invented`.
    figures: frozenset[str]


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, str | int | float) else ""


def candidate_for(profile: dict[str, Any], *, email: str) -> Candidate:
    """The candidate, or `NothingToTailorFrom`.

    Experience is required. A CV tailored from a name and a skill list would be
    a CV the model wrote for somebody it imagined.
    """
    experience = _text(profile.get("experience"))
    if not experience:
        raise NothingToTailorFrom("the profile has no experience to tailor")
    fields = {
        "current_role": _text(profile.get("currentRole")),
        "experience": experience,
        "skills": _text(profile.get("skills")),
        "education": _text(profile.get("education")),
        "certifications": _text(profile.get("certifications")),
        "achievement": _text(profile.get("keyAchievement")),
    }
    everything = " ".join([*fields.values(), _text(profile.get("aboutSection"))])
    return Candidate(
        name=_text(profile.get("name")),
        email=email.strip(),
        location=_text(profile.get("location")),
        linkedin=_text(profile.get("linkedinUrl")),
        corpus=frozenset(rules.tokens(everything)),
        figures=frozenset(rules.figures(everything)),
        **fields,
    )


def prompt(candidate: Candidate, job: dict[str, Any]) -> tuple[str, str]:
    """(system, user) for one job. `job` is the stored feed row."""
    raw = job.get("details")
    details: dict[str, Any] = raw if isinstance(raw, dict) else {}
    requirements = [str(r) for r in details.get("keyRequirements") or [] if str(r).strip()]
    description = _text(details.get("description"))[:DESCRIPTION_CHARS]

    lines = [
        "Write a tailored, ATS-optimised CV.",
        "",
        f"Target role: {_text(job.get('title'))} at {_text(job.get('company'))}",
    ]
    if requirements:
        lines.append("What the posting asks for (the ATS keywords): " + "; ".join(requirements))
    if description:
        lines += [
            "",
            "The posting itself (the source of truth for priorities):",
            '"""',
            description,
            '"""',
        ]
    lines += [
        "",
        "THE CANDIDATE — this is everything you know about them:",
        f"Current role: {candidate.current_role or '(not given)'}",
        f"Experience: {candidate.experience}",
        f"Skills: {candidate.skills or '(not given)'}",
        f"Education: {candidate.education or '(not given)'}",
        f"Certifications: {candidate.certifications or '(none)'}",
    ]
    if candidate.achievement:
        lines.append(f"Their key achievement, in their words: {candidate.achievement}")
    lines += [
        "",
        "Return an object with:",
        "- cv: an object with",
        "  - tagline (role-specific, at most 12 words, true to their background)",
        "  - summary (3 sentences: their strongest case for THIS role)",
        "  - coreSkills (up to 10 — only skills they listed, most relevant first)",
        "  - experience (array: title, company, duration, location, bullets — 3 each, "
        "impact relevant to this role — and keyImpact, one sentence)",
        '  - education (array: degree, institution, year, grade — grade "" if not given)',
        "  - certifications (only ones they listed; [] if none)",
        "  - keyAchievements (3 of their real achievements most relevant to this role)",
        "- gaps: what the posting asks for that their data does not support, as short "
        "phrases ([] if none)",
        "- outreach: an object with bestChannel, messageTemplate (a short note to the "
        "hiring manager, from what is true about them) and followUpTip",
        "",
        "Do not write their name, email, phone, location or LinkedIn — those are filled in "
        "from their account. Return ONLY the JSON object.",
    ]
    return SYSTEM, "\n".join(lines)


# ── Checking what came back ────────────────────────────────────────────────


def _strings(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if isinstance(v, str | int | float) and str(v).strip()][
        :limit
    ]


def _honest(line: str, candidate: Candidate) -> bool:
    return not rules.invented(line, candidate.figures)


def check(raw: Any, candidate: Candidate) -> tuple[dict[str, Any] | None, list[str]]:
    """(the kit, or None when what survived is not a CV; what was dropped)."""
    dropped: list[str] = []
    if not isinstance(raw, dict) or not isinstance(raw.get("cv"), dict):
        return None, ["the reply had no CV in it"]
    cv_in: dict[str, Any] = raw["cv"]

    tagline = _text(cv_in.get("tagline"))
    summary = _text(cv_in.get("summary"))
    for label, line in (("tagline", tagline), ("summary", summary)):
        if made_up := rules.invented(line, candidate.figures):
            return None, [f"the {label} cites figures the profile does not contain: {made_up}"]
    if not summary:
        return None, ["no summary"]

    experience: list[dict[str, Any]] = []
    for role in cv_in.get("experience") or []:
        if not isinstance(role, dict):
            continue
        company = _text(role.get("company"))
        if not company or not rules.supported(company, set(candidate.corpus), at_least=0.5):
            dropped.append(f"an employer the profile does not name: {company or '(blank)'}")
            continue
        bullets = []
        for bullet in _strings(role.get("bullets"), limit=4):
            if _honest(bullet, candidate):
                bullets.append(bullet)
            else:
                dropped.append(f"a bullet with an invented figure at {company}")
        impact = _text(role.get("keyImpact"))
        if impact and not _honest(impact, candidate):
            dropped.append(f"an impact line with an invented figure at {company}")
            impact = ""
        experience.append(
            {
                "title": _text(role.get("title")),
                "company": company,
                "duration": _text(role.get("duration")),
                "location": _text(role.get("location")),
                "bullets": bullets,
                "keyImpact": impact,
            }
        )
    if not experience:
        return None, [*dropped, "no experience survived"]

    corpus = set(candidate.corpus)
    skills = []
    for skill in _strings(cv_in.get("coreSkills"), limit=10):
        if rules.supported(skill, corpus):
            skills.append(skill)
        else:
            dropped.append(f"a skill the profile does not list: {skill}")
    certifications = []
    for cert in _strings(cv_in.get("certifications"), limit=10):
        if rules.supported(cert, corpus, at_least=0.6):
            certifications.append(cert)
        else:
            dropped.append(f"a certification the profile does not list: {cert}")
    achievements = []
    for item in _strings(cv_in.get("keyAchievements"), limit=4):
        if _honest(item, candidate):
            achievements.append(item)
        else:
            dropped.append("an achievement with an invented figure")

    education = [
        {
            "degree": _text(e.get("degree")),
            "institution": _text(e.get("institution")),
            "year": _text(e.get("year")),
            "grade": _text(e.get("grade")),
        }
        for e in cv_in.get("education") or []
        if isinstance(e, dict) and _text(e.get("degree"))
    ]

    outreach_raw = raw.get("outreach")
    outreach_in: dict[str, Any] = outreach_raw if isinstance(outreach_raw, dict) else {}
    message = _text(outreach_in.get("messageTemplate"))
    if message and not _honest(message, candidate):
        dropped.append("an outreach note with an invented figure")
        message = ""
    outreach = {
        "bestChannel": _text(outreach_in.get("bestChannel")),
        "messageTemplate": message,
        "followUpTip": _text(outreach_in.get("followUpTip")),
    }

    cv = {
        # Ours, not the model's. See the module docstring on the phone.
        "name": candidate.name,
        "email": candidate.email,
        "phone": "",
        "location": candidate.location,
        "linkedin": candidate.linkedin,
        "tagline": tagline,
        "summary": summary,
        "coreSkills": skills,
        "experience": experience,
        "education": education,
        "certifications": certifications,
        "keyAchievements": achievements,
    }
    kit = {
        "cv": cv,
        "gaps": _strings(raw.get("gaps"), limit=8),
        "outreach": outreach if any(outreach.values()) else None,
    }
    return kit, dropped


def write(candidate: Candidate, job: dict[str, Any], *, writer: Writer) -> dict[str, Any] | None:
    """The checked kit, or None when nothing usable came back.

    The writer's own failure propagates — `runs.execute` reports it and charges
    nothing.
    """
    system, user = prompt(candidate, job)
    kit, dropped = check(writer(system, user, MAX_TOKENS), candidate)
    if dropped:
        logger.warning("cv writer dropped %d item(s): %s", len(dropped), "; ".join(dropped))
    return kit


__all__ = [
    "DESCRIPTION_CHARS",
    "MAX_TOKENS",
    "Candidate",
    "NothingToTailorFrom",
    "Writer",
    "candidate_for",
    "check",
    "prompt",
    "write",
]
