"""Settings — the two places where the convenient answer is the wrong one.

Removing a platform must NOT delete history (a preference is not a deletion
request). Turning off resume retention MUST delete (a privacy toggle that only
flips a flag is a lie told with a switch).
"""

from __future__ import annotations

import pytest

from carigma_api.services.settings_service import (
    RESEND_COOLDOWN_SECONDS,
    DeletionReceipt,
    NoPlatformSelected,
    Platform,
    change_platforms,
    purge_resumes,
    reset_confirmation,
    set_retention,
)

BOTH = (Platform.LINKEDIN, Platform.NAUKRI)
LI = (Platform.LINKEDIN,)


class FakeStore:
    def __init__(self, files: dict[str, list[str]] | None = None, fail: set[str] | None = None):
        self.files = files or {}
        self.fail = fail or set()
        self.deleted: list[str] = []

    def list_files(self, user_id: str) -> list[str]:
        return list(self.files.get(user_id, []))

    def delete(self, user_id: str, path: str) -> bool:
        if path in self.fail:
            return False
        self.deleted.append(path)
        self.files[user_id] = [p for p in self.files.get(user_id, []) if p != path]
        return True


class ExplodingStore(FakeStore):
    def delete(self, user_id: str, path: str) -> bool:
        raise RuntimeError("storage is down")


# ── Platform preference ────────────────────────────────────────────────────


def test_adding_a_platform_reports_what_was_added() -> None:
    change = change_platforms(LI, BOTH)

    assert change.platforms == BOTH
    assert change.added == (Platform.NAUKRI,)
    assert change.removed == ()


def test_removing_a_platform_keeps_its_history_and_says_so() -> None:
    """A preference is not a deletion request. Destroying six weeks of score
    history because someone unticked a box would be irreversible."""
    change = change_platforms(BOTH, LI)

    assert change.removed == (Platform.NAUKRI,)
    assert change.retained_history_for == (Platform.NAUKRI,)

    notice = change.as_dict()["notice"]
    assert "kept, not deleted" in notice
    assert "where you left it" in notice


def test_no_notice_when_nothing_was_removed() -> None:
    assert "notice" not in change_platforms(LI, BOTH).as_dict()


def test_the_last_platform_cannot_be_removed() -> None:
    """Zero platforms leaves a product with nothing to optimise."""
    with pytest.raises(NoPlatformSelected, match="at least one"):
        change_platforms(BOTH, ())


def test_the_order_requested_does_not_change_the_result() -> None:
    a = change_platforms(LI, (Platform.NAUKRI, Platform.LINKEDIN))
    b = change_platforms(LI, (Platform.LINKEDIN, Platform.NAUKRI))
    assert a.platforms == b.platforms == BOTH


def test_duplicates_are_tolerated() -> None:
    change = change_platforms(LI, (Platform.NAUKRI, Platform.NAUKRI))
    assert change.platforms == (Platform.NAUKRI,)


def test_a_no_op_change_reports_nothing_added_or_removed() -> None:
    change = change_platforms(BOTH, BOTH)
    assert change.added == () and change.removed == ()


# ── Resume retention ───────────────────────────────────────────────────────


def test_turning_retention_off_actually_deletes() -> None:
    """The whole point. A toggle that flips a flag while the file sits in
    storage is a lie told with a switch."""
    store = FakeStore({"u1": ["u1/resume.pdf", "u1/old.pdf"]})
    change = set_retention(store, "u1", opt_in=False)

    assert store.deleted == ["u1/resume.pdf", "u1/old.pdf"]
    assert store.list_files("u1") == []
    assert change.opt_in is False
    assert change.receipt is not None
    assert change.receipt.complete is True


def test_the_deletion_receipt_carries_a_count_not_a_reassurance() -> None:
    store = FakeStore({"u1": ["u1/resume.pdf"]})
    receipt = set_retention(store, "u1", opt_in=False).receipt

    assert receipt is not None
    assert receipt.as_dict()["deleted"] == 1
    assert receipt.sentence() == "Deleted 1 stored file."


def test_deleting_nothing_says_nothing_was_stored() -> None:
    """The honest zero. A checkmark implying we removed something we never held
    is a small lie that erodes the same trust as a large one."""
    change = set_retention(FakeStore(), "u1", opt_in=False)

    assert change.receipt is not None
    assert change.receipt.sentence() == "Nothing was stored, so nothing needed deleting."
    assert change.receipt.as_dict()["deleted"] == 0


def test_a_partial_failure_leaves_the_flag_ON() -> None:
    """Recording "not retained" while files remain would make the database
    itself state a falsehood."""
    store = FakeStore({"u1": ["u1/a.pdf", "u1/b.pdf"]}, fail={"u1/b.pdf"})
    change = set_retention(store, "u1", opt_in=False)

    assert change.opt_in is True, "the flag must not claim deletion that did not happen"
    assert change.receipt is not None
    assert change.receipt.complete is False
    assert "could not be removed" in change.receipt.sentence()


def test_one_failing_path_does_not_abort_the_whole_purge() -> None:
    store = FakeStore({"u1": ["u1/a.pdf", "u1/b.pdf", "u1/c.pdf"]}, fail={"u1/b.pdf"})
    receipt = purge_resumes(store, "u1")

    assert set(receipt.deleted) == {"u1/a.pdf", "u1/c.pdf"}
    assert receipt.failed == ("u1/b.pdf",)


def test_a_storage_exception_counts_as_failed_not_deleted() -> None:
    store = ExplodingStore({"u1": ["u1/a.pdf"]})
    receipt = purge_resumes(store, "u1")

    assert receipt.deleted == ()
    assert receipt.failed == ("u1/a.pdf",)
    assert receipt.complete is False


def test_turning_retention_on_deletes_nothing() -> None:
    store = FakeStore({"u1": ["u1/a.pdf"]})
    change = set_retention(store, "u1", opt_in=True)

    assert change.opt_in is True
    assert change.receipt is None
    assert store.deleted == []


def test_each_setting_explains_what_it_actually_does() -> None:
    off = set_retention(FakeStore(), "u1", opt_in=False).as_dict()
    on = set_retention(FakeStore(), "u1", opt_in=True).as_dict()

    assert "discarded" in off["explainer"]
    assert "delete it if you turn this off" in on["explainer"]


def test_the_default_is_discard() -> None:
    """§21 Q5: analyze-and-discard by default; retention is the opt-IN."""
    from carigma_api.services.settings_service import RetentionChange

    assert RetentionChange(opt_in=False).as_dict()["resume_retention_opt_in"] is False


# ── Password reset ─────────────────────────────────────────────────────────


def test_the_reset_confirmation_persists_rather_than_flashing() -> None:
    """V1's bug: a toast that vanished before it was read, so people
    re-submitted because nothing on screen said it had worked."""
    assert reset_confirmation("a@b.com")["persistent"] is True


def test_the_reset_reply_does_not_reveal_whether_the_account_exists() -> None:
    """Confirming "no account with that email" turns the form into an
    account-enumeration oracle."""
    known = reset_confirmation("real@example.com")
    unknown = reset_confirmation("nobody@example.com")

    assert known["title"] == unknown["title"]
    # Both are conditional on the same "if".
    assert known["body"].startswith("If real@example.com has an account")
    assert unknown["body"].startswith("If nobody@example.com has an account")


def test_the_resend_cooldown_is_stated_to_the_client() -> None:
    payload = reset_confirmation("a@b.com")

    assert payload["resend_after_seconds"] == RESEND_COOLDOWN_SECONDS
    assert RESEND_COOLDOWN_SECONDS >= 30, "too short to stop mash-clicking"
    assert RESEND_COOLDOWN_SECONDS <= 300, "too long strands someone whose email never came"


def test_the_confirmation_suggests_spam_before_resending() -> None:
    assert "spam" in reset_confirmation("a@b.com")["footnote"].lower()


def test_a_receipt_with_no_requests_is_complete() -> None:
    assert DeletionReceipt((), (), ()).complete is True
