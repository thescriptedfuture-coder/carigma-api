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
}
_DB_TO_JSON = {v: k for k, v in _PROFILE_COLUMNS.items()}


def profile_to_db(profile: dict[str, Any]) -> dict[str, Any]:
    return {_PROFILE_COLUMNS[k]: v for k, v in profile.items() if k in _PROFILE_COLUMNS}


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
        except Exception:
            logger.exception("Credit balance read failed for %s", user_id)
            return None
        row = _as_row(res.data if res else None)
        if not row:
            return None
        try:
            return int(row.get("balance", 0) or 0)
        except (TypeError, ValueError):
            # A non-numeric balance is corrupt data. Returning None (fail open)
            # beats guessing 0, which would wrongly block every paid action.
            logger.error("Non-numeric credit balance for %s: %r", user_id, row.get("balance"))
            return None

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
