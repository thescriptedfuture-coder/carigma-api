"""Production builds only from `main`, and says so when it is not.

Both Render services deployed from `p4-surfaces` for weeks while every merge
went to `main`. The setting lived in a dashboard; nothing reported it. These
pin the refusal that gives it a voice, and — as carefully — the cases where a
refusal would be an outage for a reason that is not a defect.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from carigma_api.config import Settings
from carigma_api.services import deploy


def _prod(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "production",
        "supabase_url": "https://example.supabase.co",
        "supabase_service_key": "service-key-for-tests",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


# ── The rule ─────────────────────────────────────────────────────────────────


def test_production_from_another_branch_is_refused() -> None:
    reason = deploy.wrong_branch(_prod(render_git_branch="p4-surfaces"))

    assert reason is not None
    assert "p4-surfaces" in reason
    assert f"'{deploy.DEPLOY_BRANCH}'" in reason
    assert "Build & Deploy" in reason, "the refusal must say where the setting lives"


def test_production_from_main_starts() -> None:
    assert deploy.wrong_branch(_prod(render_git_branch="main")) is None


def test_a_pull_request_preview_is_not_refused_for_carrying_its_branch() -> None:
    """Render copies the parent's ENVIRONMENT onto a preview, so a preview
    reads as production with a non-main branch. That is what a preview IS."""
    assert deploy.wrong_branch(_prod(render_git_branch="p4-surfaces", is_pull_request=True)) is None


def test_outside_production_no_branch_is_refused() -> None:
    assert (
        deploy.wrong_branch(Settings(environment="test", render_git_branch="p4-surfaces")) is None
    )


def test_an_absent_branch_is_reported_not_refused() -> None:
    """Render always sets it, so absence means not-on-Render — a different
    problem from a wrong branch, surfaced by /health saying `unknown`."""
    settings = _prod(render_git_branch="")

    assert deploy.wrong_branch(settings) is None
    assert deploy.branch(settings) == "unknown"


# ── The wiring: a rule nothing calls is the orphan class again ──────────────


def test_the_api_refuses_to_start_from_the_wrong_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    from carigma_api import main as main_mod

    monkeypatch.setattr(main_mod, "get_settings", lambda: _prod(render_git_branch="p4-surfaces"))

    async def start() -> None:
        async with main_mod.lifespan(main_mod.create_app()):
            pass

    with pytest.raises(RuntimeError, match="p4-surfaces"):
        asyncio.run(start())


def test_the_cron_refuses_before_doing_anything(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Four cron jobs, each its own Render service with its own branch setting,
    and none of them has a /health. A job from the wrong branch sends real
    email from code CI has not judged."""
    spec = importlib.util.spec_from_file_location(
        "send_emails_branch_check",
        Path(__file__).resolve().parents[1] / "scripts" / "send_emails.py",
    )
    assert spec and spec.loader
    send_emails = importlib.util.module_from_spec(spec)
    sys.modules["send_emails_branch_check"] = send_emails
    spec.loader.exec_module(send_emails)

    def must_not_run(*_: object, **__: object) -> None:
        raise AssertionError("the job ran from the wrong branch")

    monkeypatch.setattr(send_emails, "Settings", lambda: _prod(render_git_branch="p4-surfaces"))
    monkeypatch.setattr(send_emails, "run", must_not_run)
    monkeypatch.setattr(send_emails, "run_reengagement", must_not_run)
    monkeypatch.setattr(send_emails, "service_client", must_not_run)

    for kind in ("daily", "weekly", "reengagement", "sweep"):
        monkeypatch.setattr(sys, "argv", ["send_emails.py", kind])
        assert send_emails.main() == 2, kind

    assert "p4-surfaces" in capsys.readouterr().err


def test_health_reports_the_branch() -> None:
    from fastapi.testclient import TestClient

    from carigma_api.config import get_settings
    from carigma_api.main import create_app

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        render_git_commit="1eb6b9f599d84c9ad82bfe4c17a556141fc3ef84", render_git_branch="main"
    )
    body = TestClient(app).get("/health").json()

    assert body["commit"] == "1eb6b9f599d8"
    assert body["branch"] == "main"
