"""Runtime configuration.

Every value is read from the environment (or a local .env that is never
committed). Nothing here has a production default that would silently "work"
with a missing secret — a misconfigured auth secret must fail loudly at
startup, not degrade into an open door.
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
    # SUPABASE_URL is the project URL; the JWKS endpoint is derived from it.
    supabase_url: str = Field(default="")
    # Legacy HS256 projects sign with the shared JWT secret. Newer projects use
    # asymmetric keys (RS256/ES256) served from JWKS. Both are supported; see
    # auth/jwt_verifier.py. Leave the secret empty when using JWKS.
    supabase_jwt_secret: str = Field(default="")
    supabase_jwks_url: str = Field(default="")
    # Expected `aud` claim. Supabase issues "authenticated" for signed-in users.
    supabase_jwt_audience: str = Field(default="authenticated")

    # ── Service role (server-only — NEVER exposed to a browser) ─────────────
    supabase_service_key: str = Field(default="")

    # ── Admin gate ─────────────────────────────────────────────────────────
    # Comma-separated allow-list. Empty ⇒ nobody is admin (safe default,
    # matching V1 config.is_admin).
    admin_emails: str = Field(default="")

    # ── CORS ───────────────────────────────────────────────────────────────
    cors_origins: str = Field(default="http://localhost:5173")

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
