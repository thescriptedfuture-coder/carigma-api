"""Supabase data access.

Ported from V1 `resumeiq/db.py`, minus Streamlit caching. Two differences that
matter:

1. **Per-request JWT.** V1 used one shared anon client for everybody, which is
   why `auth.uid()` never resolved and every V2-era table needed a permissive
   "app-server RLS" policy. Here each request builds a client carrying the
   caller's own token, so RLS actually protects rows. This is Bible §26.11.
2. **camelCase ↔ snake_case** mapping stays server-side, exactly as in V1, so
   the API's JSON shape is stable regardless of column naming.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from supabase import Client, create_client

from carigma_api.config import Settings
from carigma_api.services import credits as credits_service

logger = logging.getLogger(__name__)

# profile JSON key -> database column
_PROFILE_COLUMNS: dict[str, str] = {
    "name": "name",
    "currentRole": "current_role",
    "industry": "industry",
    "skills": "skills",
    "targetRoles": "target_roles",
    "targetRole": "target_role",
    "location": "location",
    "experience": "experience",
    "education": "education",
    "certifications": "certifications",
    "linkedinHeadline": "linkedin_headline",
    "aboutSection": "about_section",
    "niche": "niche",
    "tone": "tone",
    "postingFrequency": "posting_frequency",
    "cvTemplate": "cv_template",
    "lastSynced": "last_synced",
    "primaryGoal": "primary_goal",
    "personalBrand": "personal_brand",
    "targetSectors": "target_sectors",
    "keyAchievement": "key_achievement",
    "contentAvoid": "content_avoid",
    "linkedinUrl": "linkedin_url",
    "plan": "plan",
    "onboarded": "onboarded",
    "cadence": "cadence",
    "preferences": "preferences",
    "headshotUrl": "headshot_url",
    "platforms": "platforms",
    "resumeRetentionOptIn": "resume_retention_opt_in",
    # Real data the API could not read: 8 of 12 live profiles carry it, and
    # nothing mapped it, so "new since you last looked" had no source.
    "lastSeenJobsAt": "last_seen_jobs_at",
}
_DB_TO_JSON = {v: k for k, v in _PROFILE_COLUMNS.items()}


class UnknownProfileField(KeyError):
    """A caller used a name this mapping does not know."""


def profile_to_db(profile: dict[str, Any]) -> dict[str, Any]:
    """Translate the JSON shape into columns, REFUSING anything unrecognised.

    It used to filter silently — `if k in _PROFILE_COLUMNS` — and that is how
    `profiles.save(user_id, {"resume_retention_opt_in": ...})` became a no-op
    that returned successfully. The key is a COLUMN name, and this mapping is
    keyed on the JSON names, so the whole payload was dropped on the floor and
    the caller was told it saved. A user turning resume retention off had their
    files deleted and their stated preference discarded.

    Silently ignoring an unknown key is indistinguishable from honouring it,
    which is the same failure mode as every other rename in this codebase.
    Raising makes the mistake impossible to have without noticing.
    """
    unknown = sorted(set(profile) - set(_PROFILE_COLUMNS))
    if unknown:
        near = {
            k: next((j for j in _PROFILE_COLUMNS if _PROFILE_COLUMNS[j] == k), None)
            for k in unknown
        }
        hints = ", ".join(
            f"{k!r}" + (f" (did you mean {v!r}?)" if v else "") for k, v in near.items()
        )
        raise UnknownProfileField(f"not profile fields: {hints}")
    return {_PROFILE_COLUMNS[k]: v for k, v in profile.items()}


def db_to_profile(row: dict[str, Any]) -> dict[str, Any]:
    profile = {_DB_TO_JSON[k]: v for k, v in row.items() if k in _DB_TO_JSON}
    # NULL platforms means "never asked". Existing V1 users are LinkedIn users
    # by definition — treating NULL as linkedin avoids forcing them back through
    # onboarding just because V2 added a column.
    if profile.get("platforms") is None:
        profile["platforms"] = ["linkedin"]
    return profile


def _as_row(data: Any) -> dict[str, Any]:
    """Narrow a PostgREST payload to a single row.

    supabase-py types `.data` as a loose JSON union, so every read needs this.
    Anything that isn't an object is treated as "no row" rather than coerced —
    guessing at a malformed response is how silent data bugs start.
    """
    return dict(data) if isinstance(data, dict) else {}


def _as_rows(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        return []
    return [dict(item) for item in data if isinstance(item, dict)]


def user_client(settings: Settings, access_token: str) -> Client:
    """A Supabase client acting AS the caller.

    The user's own JWT rides on every request, so RLS sees a real `auth.uid()`
    and rows are protected by the database, not only by our code.
    """
    client = create_client(settings.supabase_url, settings.supabase_anon_key)
    client.postgrest.auth(access_token)
    return client


def service_client(settings: Settings) -> Client:
    """Service-role client — admin and cron paths ONLY. Never per-user work."""
    if not settings.supabase_service_key:
        raise RuntimeError("SUPABASE_SERVICE_KEY is not configured.")
    return create_client(settings.supabase_url, settings.supabase_service_key)


class ProfileRepository:
    def __init__(self, client: Client) -> None:
        self._client = client

    def load(self, user_id: str) -> dict[str, Any]:
        res = (
            self._client.table("profiles")
            .select("*")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        row = _as_row(res.data if res else None)
        return db_to_profile(row) if row else {}

    def save(self, user_id: str, profile: dict[str, Any]) -> dict[str, Any]:
        payload = profile_to_db(profile)
        payload["user_id"] = user_id
        payload["updated_at"] = datetime.now(UTC).isoformat()
        self._client.table("profiles").upsert(payload).execute()
        return self.load(user_id)


class ScoreRepository:
    def __init__(self, client: Client) -> None:
        self._client = client

    def record(self, user_id: str, score: int) -> None:
        self._client.table("score_history").insert(
            {"user_id": user_id, "score": int(score)}
        ).execute()

    def history(self, user_id: str, limit: int = 60) -> list[dict[str, Any]]:
        res = (
            self._client.table("score_history")
            .select("score, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return _as_rows(res.data if res else None)


class SupabaseCreditStore:
    """CreditStore backed by Supabase.

    Reads are always fresh — a balance must never be computed from a cached
    value, or a concurrent spend silently over-draws (V1 learned this).
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    def read_balance(self, user_id: str) -> int | None:
        try:
            res = (
                self._client.table("credits")
                .select("balance")
                .eq("user_id", user_id)
                .maybe_single()
                .execute()
            )
        except Exception as exc:
            logger.exception("Credit balance read failed for %s", user_id)
            # NOT None. `None` means "this user has no credits row", and a
            # failed read is not that — it used to produce a free run.
            raise credits_service.CreditsUnavailable("could not read the credit balance") from exc
        row = _as_row(res.data if res else None)
        if not row:
            return None
        try:
            # Was `int(row.get("balance", 0) or 0)`. Both defaults were
            # unreachable — the column is NOT NULL and a missing row already
            # returned None above — and both encoded "treat an absent balance
            # as zero", which is exactly what the comment below rejects. Dead
            # code that reads as a policy decision is worse than no code.
            return int(row["balance"])
        except (TypeError, ValueError) as exc:
            # Corrupt data. We do not know the balance, which is exactly what
            # CreditsUnavailable means — the old comment argued for failing
            # open "rather than guessing 0", and both options were wrong
            # because both were answers to a question we could not answer.
            logger.error("Non-numeric credit balance for %s: %r", user_id, row.get("balance"))
            raise credits_service.CreditsUnavailable("credit balance is not a number") from exc

    def apply_delta(self, user_id: str, delta: int, reason: str, balance_after: int) -> None:
        self._client.table("credits").update(
            {"balance": balance_after, "updated_at": datetime.now(UTC).isoformat()}
        ).eq("user_id", user_id).execute()
        # Ledger writes are best-effort: losing an audit row must not fail a
        # completed run, but it must be loud in the logs.
        try:
            self._client.table("credit_ledger").insert(
                {
                    "user_id": user_id,
                    "delta": int(delta),
                    "reason": (reason or "")[:80],
                    "balance_after": balance_after,
                }
            ).execute()
        except Exception:
            logger.exception("Credit ledger write failed for %s (delta %s)", user_id, delta)


def emails_by_user_id(service_db: Any) -> dict[str, str]:
    """Map user id -> email, from **auth.users**.

    `profiles` has no email column — it is on the auth user, reachable only
    with the service key. Found by running the cron for real: the query
    `profiles.select("id,email")` failed with "column profiles.email does not
    exist", and the same mistaken assumption was in two places at once. One
    helper now, so a third caller cannot repeat it.

    Note also that `profiles` is keyed by **user_id**, not `id`.
    """
    try:
        users = service_db.auth.admin.list_users()
    except Exception:
        logger.exception("could not list auth users")
        return {}
    out: dict[str, str] = {}
    for user in users or []:
        uid = getattr(user, "id", None)
        email = getattr(user, "email", None)
        if uid and email:
            out[str(uid)] = str(email)
    return out
