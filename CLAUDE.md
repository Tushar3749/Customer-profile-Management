# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An internal "Customer 360" dashboard for **GhorerBazar** (an e-commerce operation). Call-center agents look
up a customer by phone number and get one consolidated view: order history, product preferences, call/
reachability stats, delivery performance, an AI-generated relationship summary (bn/en), and reliability/
tier scoring. Supervisors/managers/super_admins get additional MIS categories and user/role management.

There is no product spec doc in-repo — the closest thing is the accumulated `CONFIRMED <date> — ...`
comments throughout `app/services/metrics.py`, `app/auth.py`, `app/routers/*.py`, and the migrations. These
record real decisions made against live production data (business thresholds, schema quirks, data-cleaning
rules) and are load-bearing — read them before changing the logic they annotate, don't just pattern-match
the surrounding code.

## Running it

```
run_all.bat            # starts backend + frontend + opens the dashboard in a browser
start_backend.bat       # venv activate + `uvicorn app.main:app --reload` (port 8000)
start_frontend.bat       # `py -m http.server 5500` served from frontend/
```

The frontend is a static HTML file with no build step or package manager — it's opened directly (file://)
or served via a plain HTTP server on a different port than the API, which is why `app/main.py` has
`allow_origins=["*"]` CORS (dev-only; must be restricted before any shared/production deployment).

Backend config comes from `.env` (see `.env.example`) via `app/config.py` (pydantic-settings): SQL Server
connection details and `GEMINI_API_KEY`. There is no test suite in this repo.

To manually refresh AI summaries: `python -m app.jobs.refresh_ai_summaries` (see that file's docstring for
flags — it's designed to run standalone now and be put on a scheduler later, mechanism TBD).

## Architecture

Layering is strict and one-directional: `routers` (thin, orchestrate) → `services` (business logic +
SQL queries) → `db.py` (raw pyodbc helpers, no ORM) → `models/schemas.py` (pydantic response contracts).
Routers assemble the final response shape; they should not contain query logic, and services should not
know about HTTP/FastAPI.

- **`app/db.py`** — `fetch_all` / `fetch_one` / `execute` / `execute_returning_identity`, each opening its
  own pyodbc connection via `get_connection()`. No connection pooling, no ORM.
- **Two databases, one SQL Server instance, no linked server**: `dcm` (this app's own tables — `users`,
  `customer_calls`, `agent_assignment_history`, `search_history`, `dashboard_ai_summary`, `roles`/
  `permissions`/`role_permissions`) and `Test` (pre-existing GhorerBazar order data, e.g.
  `Test.dbo.TestMaster`). Cross-database queries use three-part naming (`dcm.dbo.x`), never a JOIN across
  the two — there isn't a linked server to do that.
- **Auth (`app/auth.py`)** deliberately reuses GhorerBazar's *existing* login pattern rather than
  inventing a new one: bearer token = `dcm.users.current_session_token`, looked up on every request via
  `get_current_agent`. There is no token-expiry column, so none is checked — rotation/expiry happens
  wherever the token was originally issued. `/api/auth/login` (in `app/routers/auth.py`) is the one
  endpoint that does NOT depend on `get_current_agent` (nothing to check yet); `/logout` and `/me` do.
- **Permissions (`app/permissions.py`, `app/services/roles.py`)** are a layer *on top of* auth, added later
  (migration `004_add_roles_permissions.sql`), not a replacement. `require_permission(key)` still runs
  `get_current_agent` as a sub-dependency, then additionally checks the caller's resolved role against
  `role_permissions`. `users.role_id` is a nullable FK — every pre-existing user has `role_id=NULL`, and
  `resolve_role()` treats that (or any stale FK) as a soft-fallback to the `agent` role rather than an
  error, so legacy accounts keep working.
- **`app/services/metrics.py`** is the core of the domain logic: order/product/call/delivery aggregation
  queries against `TestMaster` and `dcm` tables, plus derived values computed in Python where SQL isn't
  practical (e.g. consecutive-unreachable streak). Business-rule constants (VIP/loyal/dormant/at-risk
  thresholds, escalation streak length) are named module-level constants, each tagged with the date the
  business confirmed the number — treat these as authoritative, not tunable defaults. Two recurring data-
  quality issues are handled centrally here rather than ad hoc: (1) SQL Server DECIMAL/MONEY columns come
  back from pyodbc as `Decimal` and must be normalized via `_to_float()` before mixing with Python floats;
  (2) `TestMaster` text columns have inconsistent whitespace (tabs, CRLF, NBSP/CHAR(160)) requiring the
  `_trim()` SQL fragment anywhere they're grouped/joined/compared. Order status also has ~29 raw spellings
  in production and is collapsed via `normalize_order_status()` / `_STATUS_ALIASES` into a known vocabulary,
  passing through anything unmapped rather than raising (an unmapped status used to crash the frontend's
  `renderOrders()` and silently blank out unrelated tabs — see the comment above `_STATUS_ALIASES`).
- **AI summaries (`app/services/ai_summary.py`, `app/jobs/refresh_ai_summaries.py`)**: Gemini (2.5 Flash) is
  only ever called from the background job or a manual refresh endpoint — the live `GET /api/customer/{phone}`
  path (`app/routers/customer.py`) only reads the cached fields from `dcm.dashboard_ai_summary`, never calls
  Gemini synchronously. The job's system prompt asks for bn/en variants of every field in a single call so
  it's one Gemini call per customer, not two.
- **Phone normalization (`app/services/phone.py`)** must stay in exact sync with the frontend's
  `normalizePhone()` JS — both convert to a digit-only, `880`-prefixed form. If you change one, change both.
- **`app/models/schemas.py`** defines the full `CustomerProfileResponse` contract (and sub-models like
  `Order`, `Metrics`, `AISummary`, `DeliverySummary`, `CallHistoryEntry`, etc.) that the frontend consumes —
  it's effectively the frontend/backend interface contract; check it alongside the frontend's rendering code
  when changing a field name or shape.
- **Migrations (`migrations/*.sql`)** are plain numbered SQL files, run manually against `dcm` (no migration
  tool/ORM). Each carries a header comment on responsibility and whether it's additive/backward-compatible.
- **Frontend (`frontend/GhorerBazar_Customer360_Dashboard_v2.html`)** is a single large static HTML/CSS/JS
  file (no framework, no build step) rendering the dashboard from the `CustomerProfileResponse` JSON shape;
  `login.html` and `mis_template_demo.html` are separate static pages for auth and the MIS/admin section.

## Working conventions established in this codebase

- Every module and most non-trivial functions open with a docstring stating **Responsibility** — follow
  that pattern for new modules.
- Comments prefixed `CONFIRMED <date>` record a decision verified against live data or the business —
  don't casually "clean up" or contradict these without re-verifying against the actual DB/spec.
- Prefer additive, backward-compatible schema/migration changes (nullable columns, fallback-to-default
  logic) over changes that could break existing rows/sessions — this is a live pattern in both the auth/
  permission fallback and the migration files.
