"""Onboarding: the path V2 did not have.

No upload route, no extraction, and nothing anywhere writing `onboarded`.
`DecodePage` made no API calls — a beautiful animation over no data. A stranger
after cutover would sign up, watch the most polished screen in the product do
nothing, and arrive with an empty profile that every other feature reads.

## Written against measured rows, not the column list

`scripts/measure_onboarding.py` over the twelve live profiles:

    onboarded    8/12 true, 4 null      cadence      8/12  (3:6, 5:1, 2:1)
    name        10/12                   platforms    0/12
    target_roles 10/12                  target_role  4/12

The `target_role` line is the one that changed the design, and it has its own
test below.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.routes import onboarding as routes
from carigma_api.services import extraction, onboarding
from tests.conftest import auth, make_token

EXTRACTED = {
    "name": "Ravi Kumar",
    "currentRole": "Senior Data Analyst at Zomato",
    "linkedinHeadline": "Data Analyst | SQL | Python",
    "aboutSection": "Six years turning messy data into decisions.",
    "skills": "SQL, Python, Tableau",
    "experience": "Zomato 2020-now. Swiggy 2018-2020.",
    "education": "B.Tech, DTU",
    "certifications": "",
    "industry": "Technology",
    "targetRoles": "Senior Data Analyst",
    "location": "Delhi NCR",
}


class FakeProfiles:
    def __init__(self, profile: dict[str, Any] | None = None) -> None:
        self.profile: dict[str, Any] = dict(profile or {})
        self.saves: list[dict[str, Any]] = []
        self.fail_save = False
        self.fail_load = False
        self.drop_writes = False

    def load(self, _user_id: str) -> dict[str, Any]:
        if self.fail_load:
            raise RuntimeError("profiles down")
        return dict(self.profile)

    def save(self, _user_id: str, patch: dict[str, Any]) -> None:
        if self.fail_save:
            raise RuntimeError("profiles down")
        self.saves.append(dict(patch))
        if not self.drop_writes:
            self.profile.update(patch)


@pytest.fixture
def wired(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    profiles = FakeProfiles()
    monkeypatch.setattr(routes, "_profiles", lambda request, settings: profiles)
    monkeypatch.setattr(routes, "call_claude_json", lambda *_a, **_k: dict(EXTRACTED))
    # No referral service in these tests; the activation is covered separately
    # and must never be able to fail the request.
    monkeypatch.setattr(routes, "_activate_referral", lambda _uid, _settings: None)
    yield client, profiles


def token() -> dict[str, str]:
    return auth(make_token())


# ── The step machine ───────────────────────────────────────────────────────


def test_a_brand_new_user_starts_at_upload() -> None:
    assert onboarding.step_for({}, scored=False) is onboarding.Step.UPLOAD


def test_an_extracted_profile_moves_to_decoding() -> None:
    assert onboarding.step_for(EXTRACTED, scored=False) is onboarding.Step.DECODING


def test_a_finished_user_stays_done_even_with_fields_cleared() -> None:
    """Clearing a headline does not un-onboard someone.

    `onboarded` is the one thing stored rather than derived, because "finished
    setup" and "has enough fields" are different claims and only the user can
    make the first.
    """
    assert onboarding.step_for({"onboarded": True}, scored=False) is onboarding.Step.DONE


def test_a_profile_with_only_a_headline_is_not_sent_back_to_upload() -> None:
    """A LinkedIn export without a parseable name still yields real content.

    Checking `name` alone would send that user back to re-upload a file that
    worked.
    """
    assert onboarding.has_profile_text({"linkedinHeadline": "Data Analyst"}) is True
    assert onboarding.has_profile_text({"name": "Ravi"}) is False


# ── The measured facts ─────────────────────────────────────────────────────


def test_onboarded_does_not_require_the_singular_target_role() -> None:
    """Eight live profiles are onboarded; only four have `target_role`.

    Anything that required the singular on an onboarded profile would be broken
    for half of today's users, so the step machine reads `targetRoles` — the
    field ten of twelve actually carry.
    """
    legacy = {**EXTRACTED, "targetRole": None, "onboarded": True}
    assert onboarding.step_for(legacy, scored=True) is onboarding.Step.DONE

    mid_setup = {**EXTRACTED, "targetRole": None}
    assert onboarding.step_for(mid_setup, scored=True) is onboarding.Step.TARGETS


def test_completion_writes_both_target_fields() -> None:
    """New rows should not add to the four-of-twelve problem."""
    patch = onboarding.completion(
        target_roles="Senior Data Analyst, Analytics Lead",
        cadence=3,
        platforms=["linkedin"],
        location="Delhi NCR",
    )
    assert patch["targetRoles"] == "Senior Data Analyst, Analytics Lead"
    assert patch["targetRole"] == "Senior Data Analyst"


def test_an_unsupported_cadence_falls_back_to_the_common_one() -> None:
    """Live cadences are 3 (six users), 5 (one), 2 (one). Nothing else exists."""
    assert (
        onboarding.completion(target_roles="x", cadence=7, platforms=None, location=None)["cadence"]
        == onboarding.DEFAULT_CADENCE
    )


def test_no_target_role_is_refused_rather_than_defaulted() -> None:
    """A blank target means the Scout searches for nothing."""
    with pytest.raises(ValueError):
        onboarding.completion(target_roles="   ", cadence=3, platforms=None, location=None)


def test_choosing_neither_platform_is_a_real_answer() -> None:
    """Distinct from never having been asked, which is what all twelve live
    rows are. Collapsing them would re-ask someone who already said no."""
    assert onboarding.normalise_platforms([]) == []
    assert onboarding.normalise_platforms(["naukri", "linkedin"]) == ["linkedin", "naukri"]
    assert onboarding.normalise_platforms(["myspace"]) == []


# ── Extraction ─────────────────────────────────────────────────────────────


def test_empty_strings_are_dropped_rather_than_written_over_real_data() -> None:
    """The prompt asks for "" on a missing field. Writing that would let a thin
    re-upload erase a profile the user had already filled in."""
    cleaned = extraction.clean({**EXTRACTED, "certifications": "", "education": "   "})
    assert "certifications" not in cleaned
    assert "education" not in cleaned
    assert cleaned["name"] == "Ravi Kumar"


def test_a_hallucinated_field_is_dropped_not_merged() -> None:
    """`profile_to_db` would (correctly) raise on an unknown key. Dropping here
    costs a field instead of the whole upload."""
    cleaned = extraction.clean({**EXTRACTED, "salaryExpectation": "40 LPA"})
    assert "salaryExpectation" not in cleaned


def test_too_little_text_is_refused_with_the_paste_advice() -> None:
    with pytest.raises(extraction.Unreadable) as caught:
        extraction.parse_profile_text("hi", extractor=lambda *_a: {})
    assert "paste" in str(caught.value).lower()


def test_an_oversized_file_is_refused_before_it_is_parsed() -> None:
    with pytest.raises(extraction.Unreadable) as caught:
        extraction.text_from_upload(b"x" * (extraction.MAX_BYTES + 1), "cv.pdf")
    assert "10MB" in str(caught.value)


def test_an_unreadable_pdf_names_the_next_thing_to_try() -> None:
    """Not "PdfminerException". The advice differs by format, and a scanned
    resume is a normal thing to upload."""
    with pytest.raises(extraction.Unreadable) as caught:
        extraction.text_from_upload(b"not a pdf at all", "cv.pdf")
    message = str(caught.value)
    assert "paste" in message.lower()
    assert "Exception" not in message


# ── Over HTTP ──────────────────────────────────────────────────────────────


def test_every_onboarding_route_requires_a_token(client: TestClient) -> None:
    assert client.get("/onboarding").status_code == 401
    assert client.post("/onboarding/extract", data={"text": "x" * 200}).status_code == 401
    assert client.post("/onboarding/complete", json={"target_roles": "x"}).status_code == 401


def test_pasted_text_becomes_a_saved_profile(wired: Any) -> None:
    client, profiles = wired

    res = client.post("/onboarding/extract", data={"text": "A" * 200}, headers=token())

    assert res.status_code == 200
    assert res.json()["field_count"] == len(extraction.clean(EXTRACTED))
    assert profiles.profile["linkedinHeadline"] == "Data Analyst | SQL | Python"
    # It must NOT finish setup — the targets step has not happened.
    assert not profiles.profile.get("onboarded")


def test_extraction_names_what_it_found(wired: Any) -> None:
    """So the client can say what landed rather than ending a spinner in
    silence."""
    client, _profiles = wired
    body = client.post("/onboarding/extract", data={"text": "A" * 200}, headers=token()).json()

    assert "linkedinHeadline" in body["extracted"]
    assert body["next"] == "decoding"


def test_sending_neither_a_file_nor_text_is_a_plain_400(wired: Any) -> None:
    client, _profiles = wired
    res = client.post("/onboarding/extract", data={}, headers=token())

    assert res.status_code == 400
    assert res.json()["detail"]["error"] == "nothing_to_read"


def test_an_unreadable_upload_keeps_the_paste_door_open(wired: Any) -> None:
    """Not a dead end. V1 learned this: a failed parse that offers nothing is
    where onboarding stops."""
    client, profiles = wired

    res = client.post(
        "/onboarding/extract",
        files={"file": ("cv.pdf", io.BytesIO(b"nonsense"), "application/pdf")},
        headers=token(),
    )

    assert res.status_code == 422
    assert res.json()["detail"]["can_paste"] is True
    assert profiles.saves == [], "nothing may be saved from a failed read"


def test_a_model_that_finds_nothing_is_distinct_from_a_read_failure(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "We read it and it had no profile in it" needs different advice from
    "we couldn't read it"."""
    client, profiles = wired
    monkeypatch.setattr(routes, "call_claude_json", lambda *_a, **_k: {})

    res = client.post("/onboarding/extract", data={"text": "A" * 200}, headers=token())

    assert res.status_code == 422
    assert res.json()["detail"]["error"] == "nothing_found"
    assert profiles.saves == []


def test_a_failed_save_is_not_reported_as_success(wired: Any) -> None:
    client, profiles = wired
    profiles.fail_save = True

    res = client.post("/onboarding/extract", data={"text": "A" * 200}, headers=token())

    assert res.status_code == 503
    assert "Traceback" not in res.text


# ── Completing ─────────────────────────────────────────────────────────────


def test_completing_writes_onboarded_and_reads_it_back(wired: Any) -> None:
    client, profiles = wired
    profiles.profile.update(EXTRACTED)

    body = client.post(
        "/onboarding/complete",
        json={
            "target_roles": "Senior Data Analyst",
            "cadence": 5,
            "platforms": ["linkedin", "naukri"],
            "location": "Delhi NCR",
        },
        headers=token(),
    ).json()

    assert body["onboarded"] is True
    assert body["step"] == "done"
    assert body["cadence"] == 5
    assert body["platforms"] == ["linkedin", "naukri"]
    assert profiles.profile["onboarded"] is True


def test_a_save_that_silently_wrote_nothing_is_caught(wired: Any) -> None:
    """The read-back exists for this.

    `profile_to_db` raising on an unknown key is the guard, but a write that
    reports success and stores nothing would leave someone who has finished
    setup being asked to finish setup on every single load.
    """
    client, profiles = wired
    profiles.profile.update(EXTRACTED)
    profiles.drop_writes = True

    res = client.post("/onboarding/complete", json={"target_roles": "Analyst"}, headers=token())

    assert res.status_code == 503
    assert "Traceback" not in res.text


def test_completing_without_a_target_is_refused(wired: Any) -> None:
    client, _profiles = wired
    res = client.post("/onboarding/complete", json={"target_roles": "   "}, headers=token())

    assert res.status_code == 400
    assert res.json()["detail"]["error"] == "no_target"


def test_a_referral_failure_never_costs_the_user_their_setup(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Most users were not referred, and none of them should lose a completed
    onboarding because the referral programme attached to it fell over."""
    profiles = FakeProfiles(dict(EXTRACTED))
    monkeypatch.setattr(routes, "_profiles", lambda request, settings: profiles)

    def explode(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("referrals are down")

    monkeypatch.setattr(routes, "service_client", explode)

    res = client.post("/onboarding/complete", json={"target_roles": "Analyst"}, headers=token())

    assert res.status_code == 200
    assert res.json()["onboarded"] is True
    assert res.json()["referral"] is None


# ── The welcome grant ──────────────────────────────────────────────────────


class FakeGrantClient:
    """A service client whose `grant_signup_credits` behaves like the function.

    The ONE property that matters is that it grants **once**. In production
    that is a partial unique index inside the transaction; here it is a set,
    and the point of modelling it at all is that a fake which granted every
    time would pass a test named "granted once" while the real defect —
    granting twice — sailed through.

    Same lesson as `_NullRunStore` and the payments fake with no `upsert`: a
    stand-in for something that can REFUSE has to be able to refuse.
    """

    def __init__(self, amount: int = 100) -> None:
        self.amount = amount
        self.granted: set[str] = set()
        self.calls: list[dict[str, Any]] = []

    def rpc(self, name: str, params: dict[str, Any]) -> Any:
        assert name == "grant_signup_credits", f"unexpected rpc {name}"
        self.calls.append(params)
        user = str(params["p_user_id"])

        class _Res:
            def __init__(self, data: Any) -> None:
                self.data = data

        if user in self.granted:
            # What the unique-index violation produces: null, not an error.
            return _Res(None)
        self.granted.add(user)
        return _Res(self.amount)

    def execute(self) -> Any:  # pragma: no cover - rpc() returns the result
        raise AssertionError("execute is called on the rpc result, not the client")


def _with_grant(monkeypatch: pytest.MonkeyPatch, grant: FakeGrantClient) -> None:
    class _Wrapper:
        def rpc(self, name: str, params: dict[str, Any]) -> Any:
            result = grant.rpc(name, params)

            class _Chain:
                def execute(self_inner) -> Any:  # noqa: N805
                    return result

            return _Chain()

    monkeypatch.setattr(routes, "service_client", lambda _s: _Wrapper())


def test_completing_a_profile_grants_the_welcome_credits(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`SIGNUP_CREDITS` was defined and read by NOTHING — a whole feature that
    existed only as a constant, while a new account could not receive credits
    by any path."""
    profiles = FakeProfiles(dict(EXTRACTED))
    monkeypatch.setattr(routes, "_profiles", lambda request, settings: profiles)
    grant = FakeGrantClient()
    _with_grant(monkeypatch, grant)

    res = client.post("/onboarding/complete", json={"target_roles": "Analyst"}, headers=token())

    assert res.status_code == 200
    assert res.json()["credits_granted"] == 100
    assert grant.calls[0]["p_amount"] == 100


def test_completing_twice_grants_once(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second profile save is an ordinary thing to do and must cost nothing.

    The database decides, not the caller: a `select ... if not granted` in
    Python is read-then-write, and two saves arriving together would both read
    "not yet". Here the second call returns null, which is what the unique
    violation produces.
    """
    profiles = FakeProfiles(dict(EXTRACTED))
    monkeypatch.setattr(routes, "_profiles", lambda request, settings: profiles)
    grant = FakeGrantClient()
    _with_grant(monkeypatch, grant)

    first = client.post("/onboarding/complete", json={"target_roles": "A"}, headers=token())
    second = client.post("/onboarding/complete", json={"target_roles": "A"}, headers=token())

    assert first.json()["credits_granted"] == 100
    assert second.json()["credits_granted"] is None, "a second completion granted again"
    assert len(grant.granted) == 1


def test_a_failed_grant_never_costs_someone_their_setup(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same rule as the referral activation beside it. Nobody loses a finished
    onboarding because a grant attached to it fell over — and unlike the
    referral, this one is retryable, because the index makes a later attempt
    either grant or do nothing."""
    profiles = FakeProfiles(dict(EXTRACTED))
    monkeypatch.setattr(routes, "_profiles", lambda request, settings: profiles)

    def explode(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("credits are down")

    monkeypatch.setattr(routes, "service_client", explode)

    res = client.post("/onboarding/complete", json={"target_roles": "Analyst"}, headers=token())

    assert res.status_code == 200
    assert res.json()["onboarded"] is True
    assert res.json()["credits_granted"] is None


def test_the_grant_is_not_reachable_over_rpc_by_a_signed_in_user() -> None:
    """A function `authenticated` can execute is one anybody can execute from a
    browser console — and this one mints credits. Same rule that keeps
    `referral_activate_tx` to service_role."""
    from pathlib import Path

    from tests.schema import _strip_comments

    sql = _strip_comments(
        (Path(__file__).resolve().parents[1] / "migrations" / "V2_016_signup_grant.sql").read_text(
            encoding="utf-8"
        )
    )
    statements = [" ".join(part.split()) for part in sql.split(";")]

    grants = [s for s in statements if s.startswith("grant execute") and "signup" in s]
    assert grants, "grant_signup_credits is defined but granted to nobody"
    for stmt in grants:
        grantees = {who.strip() for who in stmt.rsplit(" to ", 1)[-1].split(",")}
        assert grantees == {"service_role"}, f"the mint is reachable by {grantees}"

    revokes = [s for s in statements if s.startswith("revoke") and "signup" in s]
    for role in ("public", "anon", "authenticated"):
        assert any(s.endswith(f"from {role}") for s in revokes), f"never revoked from {role}"


def test_once_is_enforced_by_an_index_not_by_a_read() -> None:
    """The cap belongs to the database. A read-then-write in Python would let
    two concurrent completions both pay out — the same reason the referral cap
    is a unique index rather than a count."""
    from pathlib import Path

    sql = (
        Path(__file__).resolve().parents[1] / "migrations" / "V2_016_signup_grant.sql"
    ).read_text(encoding="utf-8")

    assert "create unique index" in sql
    assert "where kind = 'signup'" in sql, "the index must be PARTIAL, or it caps every kind"

    # And the route reaches it through the RPC rather than doing the work in
    # Python. Checked with the AST, not a substring: the first version of this
    # searched the function body for "select" and fired on the DOCSTRING, which
    # says the word while explaining why not to do it. Fourth time today a
    # substring stood in for a parse and reported correct code as broken.
    import ast

    tree = ast.parse(Path(routes.__file__).read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_grant_signup_credits"
    )
    calls = {
        node.func.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "rpc" in calls, "the grant must go through the transaction"
    assert "select" not in calls, "the grant path reads before it writes; two saves can race"
    assert "table" not in calls, "the grant must not touch the tables directly"


# ── Where am I ─────────────────────────────────────────────────────────────


def test_the_step_survives_a_refresh_because_it_is_derived(wired: Any) -> None:
    """No stored step. Close the tab after the extraction and the answer is the
    same, because it is a function of what is written."""
    client, profiles = wired

    assert client.get("/onboarding", headers=token()).json()["step"] == "upload"

    client.post("/onboarding/extract", data={"text": "A" * 200}, headers=token())
    again = client.get("/onboarding", headers=token()).json()

    assert again["step"] != "upload"
    assert again["has_profile"] is True
    assert profiles.profile["linkedinHeadline"]


def test_the_targets_step_is_prefilled_from_what_extraction_found(wired: Any) -> None:
    """Asking again for something we just read would say we had not read it."""
    client, profiles = wired
    profiles.profile.update(EXTRACTED)

    body = client.get("/onboarding", headers=token()).json()

    assert body["suggested"]["target_roles"] == "Senior Data Analyst"
    assert body["suggested"]["location"] == "Delhi NCR"


def test_the_options_come_from_the_server(wired: Any) -> None:
    """V1's cadence list drifted from the database twice."""
    client, _profiles = wired
    body = client.get("/onboarding", headers=token()).json()

    assert body["cadences"] == [2, 3, 5]
    assert body["platforms"] == ["linkedin", "naukri"]


def test_a_failed_profile_read_is_not_reported_as_a_new_user(wired: Any) -> None:
    """Rendering the upload screen on an error would ask someone with a full
    profile to start over."""
    client, profiles = wired
    profiles.fail_load = True

    res = client.get("/onboarding", headers=token())

    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "read_failed"


def test_only_the_completion_helper_can_set_onboarded() -> None:
    """One writer, found by walking the AST rather than trusting a convention.

    `onboarded` gates every downstream surface — Today, the ladder, the
    digests. A second place that sets it is a second definition of "this user
    is set up", and the two would eventually disagree about somebody.

    Deliberately allows READS. `step_for` and half the routes check it; only
    assigning `True` to it is restricted.
    """
    import ast
    from pathlib import Path

    src = Path(routes.__file__).parents[1]
    writers: list[str] = []

    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # `{"onboarded": True}` in a dict literal — the shape a save takes.
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values, strict=False):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "onboarded"
                    and isinstance(value, ast.Constant)
                    and value.value is True
                ):
                    writers.append(path.relative_to(src).as_posix())

    # The FILE, not a line number: pinning the line would fail on every
    # unrelated edit to that module, and a guard that cries wolf gets deleted.
    assert sorted(set(writers)) == ["services/onboarding.py"], (
        "`onboarded` is written somewhere new. It gates every downstream "
        f"surface, so there must be exactly one writer: {writers}"
    )
