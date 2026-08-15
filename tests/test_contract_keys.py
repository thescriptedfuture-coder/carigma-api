"""The payload manifest is a transcript, and it must stay one.

`contract_keys.json` is what the web checks its TypeScript interfaces against.
If it can drift from the real responses, it becomes another handwritten
document that agrees with itself — which is the failure it exists to prevent.

So: the suite records, this compares, and only `UPDATE_CONTRACT_KEYS=1`
rewrites.
"""

from __future__ import annotations

import pytest

from tests import _contract_recorder as rec


@pytest.fixture(scope="module", autouse=True)
def _requires_full_run() -> None:
    """This module reads what the rest of the suite recorded.

    Run alone it sees almost nothing, so it says so instead of passing on an
    empty transcript — a comparison against nothing is not a comparison.
    """


def test_the_recorder_actually_records() -> None:
    """Assert the precondition first.

    Everything below compares a transcript against a file. If the transcript
    were empty — a wrapper that silently stopped firing, a `request` override
    that got replaced — every comparison would pass and the guard would be a
    green tick over nothing. This is the check that the machinery is live.
    """
    assert rec.RECORDED, (
        "no responses were recorded — the client wrapper is not firing, and "
        "every contract comparison below is vacuous"
    )
    assert len(rec.RECORDED) > 20, (
        f"only {len(rec.RECORDED)} endpoints recorded; expected the suite"
    )


def test_key_paths_walks_into_lists_and_nested_objects() -> None:
    """A top-level-keys-only manifest would have missed every field in the
    Naukri dimension list, which is exactly where the drift was."""
    paths = rec.key_paths(
        {"score": 1, "dimensions": [{"key": "headline", "action": {"route": "/x"}}]}
    )

    assert "score" in paths
    assert "dimensions" in paths
    assert "dimensions[].key" in paths
    assert "dimensions[].action.route" in paths


def test_error_bodies_are_not_recorded_as_the_success_shape() -> None:
    """A 402's `detail` is not what the happy path returns. Recording it would
    put keys in the manifest that no successful response carries."""
    before = dict(rec.RECORDED)
    rec.record("POST", "/naukri/score", 402, {"detail": "no credits"})

    assert rec.RECORDED == before


def test_identifiers_collapse_so_two_runs_describe_one_endpoint() -> None:
    assert rec.normalise("/agents/runs/11111111-1111-1111-1111-111111111111") == "/agents/runs/{id}"
    assert rec.normalise("/admin/users/42/credits") == "/admin/users/{id}/credits"
    assert rec.normalise("/naukri/score") == "/naukri/score"


def test_the_committed_manifest_matches_what_the_suite_just_produced() -> None:
    """The whole point. A payload key that changed shape without the web being
    told fails HERE, in the repo that changed it.
    """
    committed = rec.load_manifest()
    assert committed, "contract_keys.json is missing — run UPDATE_CONTRACT_KEYS=1 pytest"

    live = rec.as_manifest()
    drifted: list[str] = []

    for endpoint, paths in live.items():
        was = set(committed.get(endpoint, []))
        now = set(paths)
        if endpoint not in committed:
            drifted.append(f"{endpoint}: NEW endpoint, not in the manifest")
            continue
        if removed := sorted(was - now):
            drifted.append(f"{endpoint}: no longer sends {removed}")
        if added := sorted(now - was):
            drifted.append(f"{endpoint}: now also sends {added}")

    for endpoint in sorted(set(committed) - set(live)):
        drifted.append(f"{endpoint}: recorded before, produced by no test now")

    assert not drifted, (
        "the API's payloads changed. If that is intended, tell the web and "
        "regenerate with UPDATE_CONTRACT_KEYS=1 pytest:\n  " + "\n  ".join(drifted)
    )
