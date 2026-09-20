"""Saving a profile twice. The write every surface depends on.

## The bug

`ProfileRepository.save` upserted with no conflict target. PostgREST resolves
that on the PRIMARY KEY, and the live `profiles` table's primary key is `id` —
not `user_id`, which V1's committed DDL declares and which every payload here
carries. So the conflict could never match, every save after the first became a
plain INSERT, and Postgres refused it:

    duplicate key value violates unique constraint "profiles_user_id_key"

The API turned that into a 503. Reported from the deployed build as onboarding
that could not be completed; the same call is behind changing platforms, the
analyst's profile save, retention, preferences and the unsubscribe path.

## Why nothing here caught it

Every test that exercised those routes replaced the repository wholesale with a
`FakeProfiles` that stores a dict. The layer with the bug in it was the layer
the tests stood in for — so the suite proved the routes call `save`, and never
that `save` works.

These run the REAL repository against `FakeDB`, which now knows the live
primary key and the declared unique constraints and refuses what Postgres
refuses.
"""

from __future__ import annotations

import pytest

from carigma_api.services.repository import ProfileRepository
from tests.fake_supabase import FakeDB, UniqueViolation
from tests.schema import live_primary_key

USER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def test_the_live_primary_key_is_not_the_one_the_ddl_declares() -> None:
    """The precondition, pinned. If the database is ever changed so that
    `user_id` is the primary key, this fails and the comment above becomes
    history rather than a live hazard."""
    assert live_primary_key("profiles") == ["id"]


def test_the_fake_refuses_a_second_row_for_one_user() -> None:
    """Without this the test below passes on a fake that accepts anything —
    which is exactly how the bug shipped."""
    db = FakeDB()
    db.table("profiles").insert({"user_id": USER, "name": "Ravi"}).execute()

    with pytest.raises(UniqueViolation) as caught:
        db.table("profiles").insert({"user_id": USER, "name": "Ravi again"}).execute()

    assert caught.value.code == "23505"


def test_saving_twice_updates_the_row_instead_of_colliding() -> None:
    db = FakeDB()
    profiles = ProfileRepository(db)

    profiles.save(USER, {"linkedinHeadline": "Data Analyst | SQL"})
    saved = profiles.save(USER, {"targetRoles": "Senior Data Analyst"})

    assert len(db.tables["profiles"]) == 1, "a second save must update, not insert"
    # The merge, not a replacement: completing onboarding must not wipe what the
    # extraction found.
    assert saved["linkedinHeadline"] == "Data Analyst | SQL"
    assert saved["targetRoles"] == "Senior Data Analyst"


def test_two_users_still_get_two_rows() -> None:
    """The conflict target is `user_id`, so it must separate users as well as
    join a user to themselves."""
    db = FakeDB()
    profiles = ProfileRepository(db)

    profiles.save(USER, {"name": "Ravi"})
    profiles.save(OTHER, {"name": "Someone else"})

    assert len(db.tables["profiles"]) == 2
    assert profiles.load(USER)["name"] == "Ravi"
    assert profiles.load(OTHER)["name"] == "Someone else"


def test_every_surface_that_saves_a_profile_goes_through_this() -> None:
    """The blast radius, written down. Onboarding was the reported symptom; the
    same call sits behind five more surfaces, and each of them returned a 503
    for any user who already had a profile row."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "carigma_api"
    callers = sorted(
        f"{path.name}:{i + 1}"
        for path in src.rglob("*.py")
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines())
        if "profiles.save(" in line
    )

    assert len(callers) >= 6, f"expected the known callers, found {callers}"
