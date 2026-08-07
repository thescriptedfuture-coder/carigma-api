"""Runtime configuration.

Every value is read from the environment (or a local .env that is never
committed). Nothing here has a production default that would silently "work"
with a missing secret — a misconfigured auth setup must fail loudly at startup,
not degrade into an open door.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Environment ────────────────────────────────────────────────────────
    environment: str = Field(default="development")
    debug: bool = Field(default=False)

    # ── Supabase ───────────────────────────────────────────────────────────
    supabase_url: str = Field(default="")
    supabase_anon_key: str = Field(default="")

    # Auth verification is ASYMMETRIC ONLY (JWKS / ES256). There is deliberately
    # no HS256 shared-secret setting: an HMAC secret both verifies and MINTS
    # tokens, so holding one would let this service forge a session for any
    # user. See auth/jwt_verifier.py.
    supabase_jwks_url: str = Field(default="")
    supabase_jwt_audience: str = Field(default="authenticated")
    jwks_cache_ttl_seconds: int = Field(default=600)

    # ── Service role (server-only — NEVER exposed to a browser) ─────────────
    supabase_service_key: str = Field(default="")

    # ── Anthropic ──────────────────────────────────────────────────────────
    anthropic_api_key: str = Field(default="")

    # ── Job providers ──────────────────────────────────────────────────────
    # Each is optional: an unconfigured provider is skipped, not an error, so
    # the feed degrades to whatever sources exist rather than failing.
    jsearch_api_key: str = Field(default="")
    jsearch_host: str = Field(default="jsearch.p.rapidapi.com")
    adzuna_app_id: str = Field(default="")
    adzuna_app_key: str = Field(default="")

    # V1 and V2 share ONE JSearch key during the parallel period, so V2's cap
    # is set well below the plan limit on purpose: V2 testing must not be able
    # to exhaust the quota out from under live V1 users. Raise this only after
    # cutover, when V1 is no longer drawing on the same key.
    jsearch_daily_cap: int = Field(default=25)
    jobs_cache_ttl_hours: int = Field(default=48)

    # ── Admin gate ─────────────────────────────────────────────────────────
    # Comma-separated allow-list. Empty ⇒ nobody is admin (safe default).
    admin_emails: str = Field(default="")

    # ── CORS ───────────────────────────────────────────────────────────────
    cors_origins: str = Field(default="http://localhost:5173")

    # ── Rate limits (contract §1; three distinct limiters — see §21 Q7) ────
    # 1. Authenticated agent runs, per user per agent.
    agent_run_limit_per_hour: int = Field(default=20)
    # 2. Unauthenticated public endpoints, per IP.
    public_limit_per_minute: int = Field(default=5)
    # 3. The JSearch quota is a SHARED resource handled by the jobs cache +
    #    daily cap, NOT by a per-user limiter. Do not duplicate it here.

    @property
    def admin_email_list(self) -> list[str]:
        return [e.strip().lower() for e in self.admin_emails.split(",") if e.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def jwks_url(self) -> str:
        """Resolved JWKS endpoint. Explicit setting wins; otherwise derive it
        from the Supabase project URL."""
        if self.supabase_jwks_url:
            return self.supabase_jwks_url
        if self.supabase_url:
            return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
        return ""

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
