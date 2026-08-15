"""Settings — platform preference, resume retention, and account deletion.

Two questions here have honest answers and convenient ones, and the convenient
answers are wrong in opposite directions:

1. **Removing a platform does NOT delete its history.** Turning off the Naukri
   lens is a preference, not a deletion request. Silently destroying six weeks
   of score history because someone unticked a box would be catastrophic and
   irreversible — so the data is retained and hidden, and the user is TOLD that
   before they confirm. If they want it gone, that is a separate, explicit act.

2. **Turning off resume retention DOES delete.** The opposite mistake: a toggle
   that flips a flag while the file sits in storage is a lie told with a
   switch. So the opt-out returns what was actually removed, and reports zero
   honestly when there was nothing there.

The asymmetry is deliberate. Preference changes are reversible and must not
destroy; privacy choices are promises and must be kept literally.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class Platform(StrEnum):
    LINKEDIN = "linkedin"
    NAUKRI = "naukri"


class NoPlatformSelected(ValueError):
    """At least one platform must remain — zero would leave a dead product."""


@dataclass(frozen=True)
class PlatformChange:
    """What actually changed, and what it means for the user's data."""

    platforms: tuple[Platform, ...]
    added: tuple[Platform, ...]
    removed: tuple[Platform, ...]

    @property
    def retained_history_for(self) -> tuple[Platform, ...]:
        """Removing a lens hides it; the history behind it is kept."""
        return self.removed

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "platforms": [str(p) for p in self.platforms],
            "tracks_available": [str(p) for p in self.platforms],
            "tracks_added": [str(p) for p in self.added],
            "tracks_removed": [str(p) for p in self.removed],
        }
        if self.removed:
            names = " and ".join(p.value.title() for p in self.removed)
            # Said BEFORE it becomes a surprise. The user must not discover
            # later either that we kept data they thought was gone, or that we
            # destroyed data they thought was kept.
            payload["notice"] = (
                f"Your {names} scores and history are kept, not deleted — the lens is "
                f"just hidden. Turn it back on and everything is where you left it. "
                f"To erase it, delete your account data in Settings."
            )
        return payload


def change_platforms(
    current: tuple[Platform, ...], requested: tuple[Platform, ...]
) -> PlatformChange:
    """Compute the change. Order-insensitive, duplicate-tolerant."""
    wanted = tuple(dict.fromkeys(requested))
    if not wanted:
        raise NoPlatformSelected(
            "Keep at least one platform — with none selected there is nothing to optimise."
        )

    have = set(current)
    want = set(wanted)
    canonical = tuple(p for p in (Platform.LINKEDIN, Platform.NAUKRI) if p in want)
    return PlatformChange(
        platforms=canonical,
        added=tuple(p for p in canonical if p not in have),
        removed=tuple(p for p in (Platform.LINKEDIN, Platform.NAUKRI) if p in have - want),
    )


# ── Resume retention ───────────────────────────────────────────────────────


class ResumeStore(Protocol):
    """Whatever actually holds the file. Kept abstract so the deletion path is
    testable without Supabase Storage."""

    def list_files(self, user_id: str) -> list[str]: ...

    def delete(self, user_id: str, path: str) -> bool: ...


@dataclass(frozen=True)
class DeletionReceipt:
    """What was removed. A privacy promise needs evidence, not reassurance."""

    requested: tuple[str, ...]
    deleted: tuple[str, ...]
    failed: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.failed

    def sentence(self) -> str:
        if not self.requested:
            # The honest zero. "Deleted 0 files" beats a checkmark implying we
            # removed something we never held.
            return "Nothing was stored, so nothing needed deleting."
        if self.failed:
            return (
                f"Deleted {len(self.deleted)} of {len(self.requested)}. "
                f"{len(self.failed)} could not be removed — we'll retry, and "
                f"the retention setting stays on until they are gone."
            )
        n = len(self.deleted)
        return f"Deleted {n} stored {'file' if n == 1 else 'files'}."

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": len(self.requested),
            "deleted": len(self.deleted),
            "failed": len(self.failed),
            "complete": self.complete,
            "message": self.sentence(),
        }


def purge_resumes(store: ResumeStore, user_id: str) -> DeletionReceipt:
    """Actually delete. Called when retention is switched OFF.

    Returns a receipt rather than a boolean because "we deleted your files" is
    a claim that should carry a count, and because a partial failure must be
    visible instead of rounding up to success.
    """
    paths = tuple(store.list_files(user_id))
    deleted: list[str] = []
    failed: list[str] = []
    for path in paths:
        try:
            ok = store.delete(user_id, path)
        except Exception:  # noqa: BLE001 — one bad path must not abort the purge
            ok = False
        (deleted if ok else failed).append(path)
    return DeletionReceipt(requested=paths, deleted=tuple(deleted), failed=tuple(failed))


@dataclass(frozen=True)
class RetentionChange:
    opt_in: bool
    receipt: DeletionReceipt | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            # camelCase, matching `GET /profile`. The two endpoints reported
            # the same fact under two names, so a client reading both got it
            # right for one of them.
            "resumeRetentionOptIn": self.opt_in,
            "explainer": (
                "Kept so parse checks can re-run without a re-upload. You can "
                "delete it any time, and we delete it if you turn this off."
                if self.opt_in
                else "Analysed once at upload, then discarded. Nothing is stored."
            ),
        }
        if self.receipt is not None:
            payload["deletion"] = self.receipt.as_dict()
        return payload


def set_retention(store: ResumeStore, user_id: str, *, opt_in: bool) -> RetentionChange:
    """Switching retention OFF deletes immediately.

    Not "on the next cron", not "within 30 days". The user flipped a switch
    that says their file will not be kept; the only implementation that matches
    that sentence is deleting it now.
    """
    if opt_in:
        return RetentionChange(opt_in=True)

    receipt = purge_resumes(store, user_id)
    # If deletion partially failed, the flag stays ON. Recording "not retained"
    # while files remain would make the database itself state a falsehood.
    return RetentionChange(opt_in=not receipt.complete, receipt=receipt)


# ── Email preferences ──────────────────────────────────────────────────────

#: Where email prefs live inside the `profiles.preferences` jsonb. The cron
#: already reads exactly this path, so the key is a contract between two
#: processes, not an implementation detail.
EMAIL_PREFS_KEY = "email"


@dataclass(frozen=True)
class EmailPreferences:
    """The three switches the cron honours.

    `unsubscribed_all` is a master override, not a third peer. Modelling it as
    a peer is how a UI ends up showing "daily brief: on" to someone who has
    unsubscribed — technically the stored value, and a lie about what will
    arrive.
    """

    daily_brief: bool = True
    weekly_review: bool = True
    unsubscribed_all: bool = False

    @classmethod
    def from_stored(cls, stored: Any) -> EmailPreferences:
        """Read from the jsonb. Absent means opted IN, matching the cron.

        Defaults are duplicated here and in `emails.Preferences` because the two
        run in different processes; `test_defaults_match_the_cron` pins them
        together so a change in one fails loudly rather than silently mailing
        someone who had opted out.
        """
        prefs = stored if isinstance(stored, dict) else {}
        return cls(
            daily_brief=bool(prefs.get("daily_brief", True)),
            weekly_review=bool(prefs.get("weekly_review", True)),
            unsubscribed_all=bool(prefs.get("unsubscribed_all", False)),
        )

    def receives(self, name: str) -> bool:
        """What will ACTUALLY arrive — the override applied."""
        if self.unsubscribed_all:
            return False
        return self.daily_brief if name == "daily_brief" else self.weekly_review

    def as_dict(self) -> dict[str, Any]:
        return {
            "daily_brief": self.daily_brief,
            "weekly_review": self.weekly_review,
            "unsubscribed_all": self.unsubscribed_all,
            # Sent so the client never has to re-derive the override rule. Two
            # implementations of one rule is how they drift apart.
            "effective": {
                "daily_brief": self.receives("daily_brief"),
                "weekly_review": self.receives("weekly_review"),
            },
            # Stated on the surface, because someone who turns everything off
            # and then receives a payment receipt will reasonably think we
            # ignored them. A receipt is a transaction record, not marketing.
            "still_sent": "Payment receipts and account emails still arrive — those aren't marketing.",
        }


def merge_email_preferences(stored_preferences: Any, prefs: EmailPreferences) -> dict[str, Any]:
    """Fold email prefs into the WHOLE preferences blob.

    `profiles.preferences` also holds the Profile Analyst's learned user memory.
    `ProfileRepository.save` writes the column wholesale, so writing
    `{"email": ...}` would erase that memory — silently, and only noticed weeks
    later when the agent stopped honouring things the user had told it.
    """
    merged = dict(stored_preferences) if isinstance(stored_preferences, dict) else {}
    merged[EMAIL_PREFS_KEY] = {
        "daily_brief": prefs.daily_brief,
        "weekly_review": prefs.weekly_review,
        "unsubscribed_all": prefs.unsubscribed_all,
    }
    return merged


# ── Password reset ─────────────────────────────────────────────────────────

#: Long enough to stop mash-clicking, short enough not to strand someone whose
#: first email genuinely did not arrive.
RESEND_COOLDOWN_SECONDS = 60


def reset_confirmation(email: str) -> dict[str, Any]:
    """The same answer whether or not the address is registered.

    Confirming "no account with that email" turns the reset form into an
    account-enumeration oracle. The copy is deliberately about the ACTION we
    took, not about what we found.
    """
    return {
        # Persistent — the V1 bug was a toast that vanished before it was read,
        # leaving people re-submitting because nothing on screen said it worked.
        "persistent": True,
        "title": "Check your email",
        "body": (
            f"If {email} has an account, a reset link is on its way. It expires in 60 minutes."
        ),
        "resend_after_seconds": RESEND_COOLDOWN_SECONDS,
        "footnote": "Nothing yet? Check spam before resending.",
    }
