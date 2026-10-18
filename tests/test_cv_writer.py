"""CV Architect: a CV an employer reads, held to what the person told us.

Most of this file is about what must NOT survive, because the CV leaves the
product with someone's name at the top of it.
"""

from __future__ import annotations

from typing import Any

import pytest

from carigma_api.services.cv_writer import (
    NothingToTailorFrom,
    candidate_for,
    check,
    prompt,
    write,
)

PROFILE: dict[str, Any] = {
    "name": "Ravi Kumar",
    "location": "Delhi NCR",
    "linkedinUrl": "linkedin.com/in/ravi",
    "currentRole": "Senior Auditor / Functional Business Analyst",
    "experience": (
        "Controller General Of Defence Accounts (CGDA) | Senior Auditor, 2016 - now. "
        "Led the PFMS rollout; cut monthly reconciliation time by 40%."
    ),
    "skills": "SQL, Power BI, Python, Requirements Analysis, Tableau",
    "education": "University of Delhi — B.A. Honours in Business Economics",
    "certifications": "Google Data Analytics; Share Data Through the Art of Visualization (Google)",
}

JOB: dict[str, Any] = {
    "title": "Senior Data Analyst",
    "company": "Zomato",
    "details": {
        "keyRequirements": ["SQL", "Power BI", "Stakeholder management", "dbt"],
        "description": "We want an analyst who owns dashboards end to end.",
    },
}


def candidate() -> Any:
    return candidate_for(PROFILE, email="ravi@example.com")


def reply(**cv: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "tagline": "Audit-trained analyst who turns ledgers into decisions",
        "summary": "Eight years reconciling government accounts. Builds the SQL and Power BI "
        "behind decisions. Brings audit discipline to analytics.",
        "coreSkills": ["SQL", "Power BI", "Python"],
        "experience": [
            {
                "title": "Senior Auditor",
                "company": "Controller General Of Defence Accounts (CGDA)",
                "duration": "2016 — now",
                "location": "Delhi",
                "bullets": [
                    "Led the PFMS rollout across field offices",
                    "Cut monthly reconciliation time by 40%",
                    "Built the SQL checks the audit relied on",
                ],
                "keyImpact": "Made reconciliation a report instead of a ritual.",
            }
        ],
        "education": [
            {
                "degree": "B.A. Honours, Business Economics",
                "institution": "DU",
                "year": "",
                "grade": "",
            }
        ],
        "certifications": ["Google Data Analytics"],
        "keyAchievements": ["Led the PFMS rollout"],
    }
    base.update(cv)
    return {
        "cv": base,
        "gaps": ["dbt", "Stakeholder management at a product company"],
        "outreach": {
            "bestChannel": "LinkedIn",
            "messageTemplate": "I've spent eight years making government ledgers reconcile.",
            "followUpTip": "Follow up after four working days.",
        },
    }


# ── What it may build from ─────────────────────────────────────────────────


def test_a_profile_with_no_experience_is_refused() -> None:
    with pytest.raises(NothingToTailorFrom):
        candidate_for({"name": "Someone", "skills": "SQL"}, email="x@example.com")


def test_the_prompt_carries_the_posting_the_candidate_and_the_rules() -> None:
    system, user = prompt(candidate(), JOB)

    assert "NEVER fabricate" in system
    assert "Senior Data Analyst at Zomato" in user
    assert "dbt" in user
    assert "owns dashboards end to end" in user
    assert "PFMS rollout" in user


def test_a_posting_with_no_stored_description_is_tailored_to_what_there_is() -> None:
    """Every V2-scanned row before this change has no description stored."""
    _system, user = prompt(candidate(), {**JOB, "details": {"keyRequirements": ["SQL"]}})

    assert "The posting itself" not in user
    assert "SQL" in user


# ── What it may not ship ───────────────────────────────────────────────────


def test_an_honest_cv_survives_with_contact_details_from_the_account() -> None:
    kit, dropped = check(reply(), candidate())

    assert kit is not None
    assert dropped == []
    cv = kit["cv"]
    assert (cv["name"], cv["email"], cv["location"]) == (
        "Ravi Kumar",
        "ravi@example.com",
        "Delhi NCR",
    )
    # V1 printed "+91 XXXXX XXXXX" on a document sent to employers.
    assert cv["phone"] == ""
    assert kit["gaps"] == ["dbt", "Stakeholder management at a product company"]
    assert kit["outreach"]["bestChannel"] == "LinkedIn"


def test_contact_details_the_model_wrote_are_ignored() -> None:
    kit, _ = check(
        reply(name="Someone Else", email="invented@example.com", phone="+91 98765 43210"),
        candidate(),
    )

    assert kit is not None
    assert kit["cv"]["name"] == "Ravi Kumar"
    assert kit["cv"]["email"] == "ravi@example.com"
    assert kit["cv"]["phone"] == ""


def test_an_employer_the_profile_does_not_name_is_dropped_with_its_role() -> None:
    roles = reply()["cv"]["experience"] + [
        {"title": "Data Lead", "company": "Infosys", "bullets": ["Ran a team of analysts"]}
    ]

    kit, dropped = check(reply(experience=roles), candidate())

    assert kit is not None
    assert [r["company"] for r in kit["cv"]["experience"]] == [
        "Controller General Of Defence Accounts (CGDA)"
    ]
    assert any("Infosys" in d for d in dropped)


def test_a_bullet_with_an_invented_figure_is_dropped_and_the_real_one_kept() -> None:
    role = dict(reply()["cv"]["experience"][0])
    role["bullets"] = [
        "Cut monthly reconciliation time by 40%",
        "Saved ₹3 crore in duplicate payments",
    ]

    kit, dropped = check(reply(experience=[role]), candidate())

    assert kit is not None
    assert kit["cv"]["experience"][0]["bullets"] == ["Cut monthly reconciliation time by 40%"]
    assert any("invented figure" in d for d in dropped)


@pytest.mark.parametrize("field", ["summary", "tagline"])
def test_an_invented_figure_in_the_first_lines_fails_the_whole_cv(field: str) -> None:
    """The first thing an employer reads. Not trimmed — refused, and free."""
    kit, dropped = check(
        reply(**{field: "Improved team productivity by 65% in a year."}), candidate()
    )

    assert kit is None
    assert field in dropped[0]


def test_a_skill_or_certification_the_profile_does_not_list_is_dropped() -> None:
    kit, dropped = check(
        reply(
            coreSkills=["SQL", "Snowflake", "Data Visualisation"],
            certifications=["Google Data Analytics", "AWS Certified Data Engineer"],
        ),
        candidate(),
    )

    assert kit is not None
    # "Visualisation" against the profile's "Visualization": one word, two spellings.
    assert kit["cv"]["coreSkills"] == ["SQL", "Data Visualisation"]
    assert kit["cv"]["certifications"] == ["Google Data Analytics"]
    assert any("Snowflake" in d for d in dropped)
    assert any("AWS" in d for d in dropped)


def test_no_surviving_experience_is_not_a_cv() -> None:
    kit, dropped = check(reply(experience=[{"title": "CEO", "company": "Acme Corp"}]), candidate())

    assert kit is None
    assert "no experience survived" in dropped


@pytest.mark.parametrize("raw", [None, "text", [], {"cv": "x"}, {"cv": {"summary": ""}}])
def test_a_reply_that_is_not_a_cv_yields_none(raw: Any) -> None:
    assert check(raw, candidate())[0] is None


def test_an_outreach_note_with_an_invented_figure_is_blanked_not_shipped() -> None:
    raw = reply()
    raw["outreach"]["messageTemplate"] = "I grew revenue 3x at my last role."

    kit, _ = check(raw, candidate())

    assert kit is not None
    assert kit["outreach"]["messageTemplate"] == ""


def test_write_returns_none_when_nothing_usable_came_back() -> None:
    assert write(candidate(), JOB, writer=lambda *_a: {"cv": {}}) is None


def test_a_dead_writer_raises_rather_than_returning_nothing() -> None:
    def dead(*_a: Any) -> Any:
        raise RuntimeError("anthropic is down")

    with pytest.raises(RuntimeError):
        write(candidate(), JOB, writer=dead)


def test_a_decorated_employer_survives_and_a_different_one_does_not() -> None:
    """Ravi's one employer is "Controller General Of Defence Accounts (CGDA)".
    A model adding "Ministry of Defence" must not lose it — and "Ministry of
    Finance" alone is not it."""
    good = dict(reply()["cv"]["experience"][0])
    good["company"] = "Controller General of Defence Accounts (CGDA), Ministry of Defence"
    other = {"title": "Analyst", "company": "Ministry of Finance", "bullets": ["Wrote reports"]}

    kit, _ = check(reply(experience=[good, other]), candidate())

    assert kit is not None
    assert [r["company"] for r in kit["cv"]["experience"]] == [good["company"]]


def test_a_real_certification_with_its_full_title_survives() -> None:
    kit, _ = check(
        reply(certifications=["Google Data Analytics Professional Certificate"]), candidate()
    )

    assert kit is not None
    assert kit["cv"]["certifications"] == ["Google Data Analytics Professional Certificate"]
