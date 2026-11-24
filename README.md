# Agentic Workflow Assistant

A multi-agent assistant that turns natural-language requests into Gmail and Google Calendar actions
(FastAPI · PostgreSQL · SQLAlchemy 2.0 · LangGraph · MCP · React).

## Delivery plan (3 phases per batch)

| Batch | Phases | Status |
|---|---|---|
| 1 | 1 Setup/architecture · 2 FastAPI + PostgreSQL · 3 Google OAuth + JWT | **Done, verified by you** |
| 2 | 4 Gmail API · 5 Calendar API · 6 LLM router + intent recognition | **Done (this drop)** |
| 3 | 7 Service selector + context enhancement · 8 Operation agents · 9 LangGraph | Approval workflow and operation execution implemented; LangGraph remains |
| 4 | 10 MCP servers/client · 11 React frontend · 12 Tests, Docker, evaluation (200+ queries), docs | React, Docker, and tests implemented; MCP/evaluation remain |

## What exists after Phases 1-3

- **Config**: validated settings (`app/core/config.py`); app refuses to start with a weak `JWT_SECRET` or invalid Fernet key.
- **Database**: models for users, OAuth credentials, conversations, messages, session context and workflow executions,
  with FKs, `ON DELETE CASCADE`, check constraints, unique constraints (incl. an idempotency key for duplicate-action
  prevention, used from Phase 8). Alembic migration `0001`.
- **Google OAuth 2.0 (Authorization Code, offline access)**: CSRF protection (signed `state` + httpOnly nonce cookie),
  ID-token verification (signature, audience, `email_verified`), Gmail + Calendar scopes, user upsert.
- **Token handling**: access/refresh tokens are Fernet-encrypted at rest and never sent to the browser. A token
  service refreshes expiring tokens, keeps the old refresh token if Google omits a new one, and marks credentials
  `revoked` on `invalid_grant`. Phases 4-5 will call `get_valid_access_token()`.
- **JWT sessions**: short-lived app JWT delivered in the URL *fragment* after login; `type` claims stop state tokens
  being used as sessions; `alg=none` is rejected.
- **API** (`/api`): `GET /health`, `GET /health/ready`, `GET /auth/google/login`, `GET /auth/google/callback`,
  `GET /auth/me` (profile + Gmail/Calendar connection status), `POST /auth/logout`,
  `DELETE /auth/google/connection` (revoke + delete), and conversations `POST/GET/GET{id}/DELETE`
  with strict per-user isolation (other users' IDs return 404).

## What Batch 2 adds (Phases 4-6)

**Gmail (`app/integrations/gmail`)**: REST client (search, read, thread, send, draft), safe MIME building
(header-injection and address-list rejection, UTF-8), reply threading (`threadId`, `In-Reply-To`, `References`,
`Re:`), HTML-to-text fallback, attachment listing. `GmailService` re-checks every send/draft against Gmail
(`SENT` label / draft exists / thread preserved) and reports `verified: false` rather than claiming success.
Replies go to the message's `From`, never `Reply-To`, so an attacker cannot redirect a reply.
All email content is returned with `untrusted: true`.

**Calendar (`app/integrations/calendar`)**: REST client (list, get, create, patch, delete, free/busy),
time-zone-aware helpers (naive times are localized, DST gaps rejected), event **matching** that returns
`unique` / `ambiguous` / `none` instead of guessing, and `CalendarService` that re-reads after every write.
Creation is idempotent via a deterministic event id derived from an idempotency key. Cancellation is verified,
and a failed verification raises an error instead of a success message.

**Retry policy (`integrations/http.py`)**: reads retry on 429/5xx/network errors; writes such as *send* retry
only on 429 (request rejected), so a timeout can never produce a duplicate email.

**LLM layer (`app/services/llm`)**: OpenRouter (default: `deepseek/deepseek-r1`) and Gemini providers, a
role-based `LLMRouter` (intent / summarization / task_automation, each overridable by env), structured output
(strips R1 `<think>` blocks, extracts JSON, validates with Pydantic, feeds validation errors back to the model up
to 3 attempts), and versioned prompt templates. `GET /api/llm/config` reports what is *actually* configured
(`distinct_models` is 1 until you set overrides; nothing is simulated).

**Intent Recognition Agent (`app/agents/intent_agent.py`)**: returns a validated `IntentResult`. Which
parameters are missing is computed deterministically from the entities (not trusted from the LLM). A person's name
is never accepted as an email address. If the model's output is unusable the agent asks the user to rephrase
instead of guessing; provider outages are surfaced as errors, not masked.

### New endpoints
| Endpoint | Purpose |
|---|---|
| `GET /api/gmail/messages?q=` · `/messages/{id}` · `/threads/{id}` | read-only Gmail |
| `GET /api/calendar/events?days=&q=` · `/next` · `/availability?start=&end=` | read-only Calendar |
| `POST /api/intent/analyze` `{message, context?}` | classify a message; **performs no action** |
| `GET /api/llm/config` | provider/model per role (no secrets) |
| `PATCH /api/auth/me/timezone` `{timezone}` | IANA zone used to resolve dates |
| `POST /api/workflows` · `GET /api/workflows` | Propose an approved Gmail/Calendar action and list executions |
| `POST /api/workflows/{id}/decision` · `DELETE /api/workflows/{id}` | Approve/execute or reject an action |

Write operations are only reachable through the approval workflow. A proposal is stored as
`awaiting_approval`; approval executes the verified Gmail/Calendar service and stores the result, while rejection
stores `rejected`. The `idempotency_key` prevents duplicate proposals for the same user.

### Manual testing of Batch 2
```bash
TOKEN=...   # from the Google login flow (see Batch 1)
H="Authorization: Bearer $TOKEN"
curl -X PATCH -H "$H" -H 'Content-Type: application/json' -d '{"timezone":"Asia/Kolkata"}' localhost:8000/api/auth/me/timezone
curl -H "$H" "localhost:8000/api/gmail/messages?q=is:unread&max_results=5"
curl -H "$H" localhost:8000/api/calendar/next
curl -H "$H" "localhost:8000/api/calendar/availability?start=2026-11-25T09:00&end=2026-11-25T18:00"
curl -H "$H" localhost:8000/api/llm/config
# needs OPENROUTER_API_KEY in .env (R1 can take 10-60 s):
curl -X POST -H "$H" -H 'Content-Type: application/json' \
     -d '{"message":"Move my meeting with Rahul to Friday at 3 PM"}' localhost:8000/api/intent/analyze
curl -X POST -H "$H" -H 'Content-Type: application/json' \
     -d '{"message":"Send an email to Professor saying I will miss class"}' localhost:8000/api/intent/analyze
#   -> missing_parameters ["recipient_email"], requires_clarification true
```
Using Gemini instead: `LLM_PROVIDER=gemini`, `GEMINI_API_KEY=...` **and** set `LLM_MODEL` to a current Gemini model
name (the default model name is for OpenRouter).

## Setup

### 1. Environment
```bash
cd backend
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"                       # -> JWT_SECRET
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # -> TOKEN_ENCRYPTION_KEY
```

### 2. Google OAuth
1. [Google Cloud Console](https://console.cloud.google.com) → create/select a project.
2. **APIs & Services → Library**: enable **Gmail API** and **Google Calendar API**.
3. **OAuth consent screen**: External (or Internal), add yourself as a test user, add scopes
   `openid`, `email`, `profile`, `.../auth/gmail.modify`, `.../auth/calendar`.
   (Gmail/Calendar scopes are *sensitive/restricted*: while in "Testing" mode, refresh tokens expire after 7 days.)
4. **Credentials → Create OAuth client ID → Web application**; authorized redirect URI:
   `http://localhost:8000/api/auth/google/callback`.
5. Put the client ID/secret into `.env`.

### 3. Database
```bash
docker compose up -d db                 # PostgreSQL 16
cd backend && python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
```

### 4. Run
```bash
uvicorn app.main:app --reload            # http://localhost:8000/docs
```
Try the flow by opening `http://localhost:8000/api/auth/google/login` in a browser; you are redirected to
`FRONTEND_URL/auth/callback#token=...` (the React app arrives in Phase 11; until then copy the token and use it as
`Authorization: Bearer <token>` against `/api/auth/me`).

### Docker (backend + db)
```bash
cp backend/.env.example backend/.env   # fill in secrets
docker compose up --build              # runs migrations, then serves on :8000
```

Compose now starts the React client at `http://localhost:5173` after the backend readiness check passes.
The first login requires Google OAuth credentials and the authorized callback URI shown above. If an older
`pgdata` volume was created with different PostgreSQL credentials, recreate it once with
`docker compose down -v` followed by `docker compose up --build` (this deletes only the Compose database volume).

### Tests
```bash
cd backend && pytest -v
```
Tests use in-memory SQLite and mocked Google calls; no credentials needed.

## Test status

The complete backend suite runs locally with the project virtual environment:

| Suite | Tests | Status |
|---|---|---|
| `test_validation`, `test_mime`, `test_timeutil`, `test_matching`, `test_jsonutil` (stdlib only) | 32 | **executed here: all pass** |
| `test_gmail_service`, `test_calendar_service` (fake clients, `httpx` stubbed) | 21 | **executed here: all pass** |
| Complete `backend/app/tests` suite | 125 | **pass** |
| Frontend production build (`npm run build`) | | **pass** |
| Compose configuration (`docker compose config`) | | **pass** |

No real Gmail, Calendar or LLM call can be made until credentials are supplied in `backend/.env`; the
200-query intent evaluation is still Phase 12 and has no accuracy figure yet.

## Known limitations (so far)
- Sessions are stateless JWTs: logout is client-side and a token stays valid until expiry (default 8 h).
- Alembic migration is hand-written; run `alembic check` locally to confirm it matches the models.
- Sync SQLAlchemy/httpx is used deliberately (simple, adequate for this workload).
- Relative dates ("tomorrow", "next Monday") are extracted as the user's wording; resolution against a reference
  date is Phase 7. Only the intent agent exists so far (no service selector, context enhancement or execution agents).
- Calendar listing reads one page (max 250 events in the window); all-day events cannot be rescheduled yet.
- Calendar writes use `sendUpdates=none` by default (attendees are not emailed) until the approval flow decides.
- Gmail search makes 1 + N requests (N <= 25). On a mid-request 401 the user is asked to reconnect.
- DeepSeek R1 is slow and its reasoning tokens count against `LLM_MAX_TOKENS` (default 4096); raise it if you
  see "empty content" errors.
- LangGraph orchestration, MCP integration, and 200-query evaluation remain planned phases. The React client
  exposes the implemented read-only and intent-analysis API surface; write actions are available through the
  approval API and remain intentionally absent from the raw Gmail/Calendar routes.
