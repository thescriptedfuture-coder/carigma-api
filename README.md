# carigma-api

Carigma V2 backend — FastAPI. Wraps the existing Carigma intelligence layer
(agents, prompts, scoring, jobs engine) behind a real HTTP API.

> **P1 scope: foundations only.** Config, JWT auth, health, CI.
> **No business logic is ported yet** — that is P2. The agent/prompt/scoring
> code is the IP and ports over intact (roadmap Part 3); it is deliberately not
> touched here.

## Quick start

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows
cp .env.example .env
.venv/Scripts/python.exe -m uvicorn carigma_api.main:app --reload
```

Docs at `http://localhost:8000/docs` (disabled in production).

## Auth model

```
React holds the Supabase session
  → sends the JWT on every request
  → FastAPI verifies it on every protected endpoint
  → authorized service call
```

`src/carigma_api/auth/` is the security core:

| Piece | Guarantee |
|---|---|
| `jwt_verifier.py` | Signature always verified. `exp`/`aud`/`iss` validated. `alg` pinned to an allow-list, so `none` and algorithm-confusion downgrades are rejected. Supports legacy HS256 secrets and current JWKS projects. |
| | **Every failure returns an identical body** — distinguishable errors are free reconnaissance for an attacker. |
| `dependencies.py` | `CurrentUser` (any verified user), `AdminUser` (verified + on `ADMIN_EMAILS`), `ServiceRole` (server-to-server, constant-time key compare). |
| `assert_owns()` | Authentication is not authorization. Any route loading a row by id must call this, or one user can read another's career data by guessing an id. |

Empty `ADMIN_EMAILS` means **nobody** is admin — a safe default, matching V1.

In production the app **refuses to start** without a JWT verification method
configured, rather than running as an open door.

## Testing

```bash
.venv/Scripts/python.exe -m pytest
```

The auth suite treats failure paths as first-class: missing token, wrong scheme,
`alg: none`, unlisted algorithm, tampered payload, wrong secret, expired, wrong
audience, wrong issuer, missing `sub`/`exp`, `anon` role, non-admin escalation,
self-declared admin claims, and cross-user access. Each test names the attack it
prevents.

## Commands

| Command | |
|---|---|
| `ruff check .` | lint |
| `ruff format .` | format |
| `mypy src` | types (strict) |
| `pytest` | tests |

CI runs all four.

## What comes next (P2)

Port `resumeiq/` into `src/carigma_api/services/` as stateless
`service(input) -> output` functions and expose them per
[`../CARIGMA_V2_API_CONTRACT.md`](../CARIGMA_V2_API_CONTRACT.md). Reuse the
prompts verbatim — they are the IP.
