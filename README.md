# Agentic Workflow Assistant

A conversational assistant that turns natural-language requests into **safe, context-aware Gmail and Google Calendar actions**.

You sign in with Google and type requests like *"email Priya that the demo moved to Friday"* or *"move my sync with Arjun to 3pm tomorrow"*. A LangGraph pipeline of specialized agents works out what you mean, asks for anything missing, and gets data from Google through **Model Context Protocol (MCP)** servers. Anything that writes to Google (sending mail, creating or changing events) waits in an **approval queue** and only runs after you approve it.

| Layer | Technology |
|---|---|
| API | Python 3.12, FastAPI, Pydantic 2, PyJWT |
| Orchestration | LangGraph (state graph with conditional routing) |
| Tools | Official Python MCP SDK, with separate Gmail and Calendar servers |
| LLMs | OpenRouter (DeepSeek R1 by default) and Google Gemini, routed by role |
| Persistence | PostgreSQL 16, SQLAlchemy 2.0, Alembic |
| Security | Google OAuth 2.0 (Authorization Code flow), Fernet-encrypted tokens, JWT sessions |
| Frontend | React, Vite, Lucide icons |
| Runtime | Docker Compose (db, backend, frontend) |

---

## Table of contents

1. [Features](#features)
2. [Quick start (Docker)](#quick-start-docker)
3. [Architecture](#architecture)
   - [System overview](#system-overview)
   - [Request lifecycle](#request-lifecycle)
   - [LangGraph workflow](#langgraph-workflow)
   - [Agents](#agents)
   - [MCP layer](#mcp-layer)
   - [Multi-model LLM routing](#multi-model-llm-routing)
   - [Approval workflow](#approval-workflow)
   - [Conversation context and memory](#conversation-context-and-memory)
   - [Data model](#data-model)
   - [Security model](#security-model)
4. [Project structure](#project-structure)
5. [Local development setup](#local-development-setup)
6. [Configuration reference](#configuration-reference)
7. [API reference](#api-reference)
8. [Example conversation](#example-conversation)
9. [Testing](#testing)
10. [Extending the assistant](#extending-the-assistant)
11. [Troubleshooting and error codes](#troubleshooting-and-error-codes)
12. [Known limitations](#known-limitations)

---

## Features

- **Natural-language Gmail:** search, read, summarize, reply, draft and send email.
- **Natural-language Google Calendar:** list events, check availability, create, reschedule and cancel events.
- **Multi-agent orchestration:** a Service Selector, Intent Recognition, Context Enhancement and per-operation Gmail and Calendar agents, wired together with LangGraph.
- **MCP tool layer:** Gmail and Calendar are exposed as MCP servers with 12 tools in total. Agents never call Google directly.
- **Human-in-the-loop safety:** every write operation becomes an `awaiting_approval` workflow, and nothing is sent, created or changed until you approve it.
- **Multi-turn context:** the assistant resolves pronouns ("him", "that meeting"), relative dates ("next Tuesday") in your timezone, earlier candidate selections, and half-finished requests across turns.
- **Deterministic validation:** the LLM extracts entities, but plain code decides which parameters are missing, so a model cannot skip a required field.
- **Role-based model routing:** intent recognition, summarization and email composition can each use a different provider and model.
- **Honest reporting:** an action is reported as done only when an MCP tool has confirmed it. Errors are classified and explained to the user.
- **Full traceability:** each assistant message stores the node trace, LLM calls (role, provider, model) and MCP calls (server, tool, outcome, duration). Tool arguments are never logged.

---

## Quick start (Docker)

Prerequisites: Docker Desktop, plus Google OAuth credentials and an OpenRouter or Gemini API key for live use.

```powershell
# 1. Create the backend env file
Copy-Item backend\.env.example backend\.env

# 2. Generate the two required secrets and paste them into backend\.env
python -c "import secrets; print(secrets.token_urlsafe(48))"                               # JWT_SECRET
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # TOKEN_ENCRYPTION_KEY

# 3. Add GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET and OPENROUTER_API_KEY or GEMINI_API_KEY

# 4. Start everything (the backend applies Alembic migrations on boot)
docker compose up --build
```

| Service | URL |
|---|---|
| Frontend | http://localhost:5173 |
| Backend API | http://localhost:8000/api |
| Interactive API docs (Swagger) | http://localhost:8000/docs |
| PostgreSQL | `localhost:5432` (user, password and database are all `assistant`) |

See [Google OAuth configuration](#google-oauth-configuration) to register the redirect URI.

---

## Architecture

### System overview

```mermaid
flowchart LR
    subgraph Browser
        UI[React + Vite workspace]
    end

    subgraph Backend["FastAPI backend"]
        API[REST routes<br/>JWT auth]
        WF[LangGraph workflow]
        AG[Specialized agents]
        LLM[LLM router]
        GW[MCP gateway<br/>client]
        APR[Approval service]
    end

    subgraph MCP["MCP servers (stdio subprocesses)"]
        GS[Gmail MCP server]
        CS[Calendar MCP server]
    end

    DB[(PostgreSQL)]
    OR[OpenRouter / DeepSeek]
    GE[Google Gemini]
    GA[Gmail API]
    GC[Google Calendar API]
    GO[Google OAuth]

    UI -- Bearer JWT --> API
    API --> WF --> AG
    AG --> LLM
    LLM --> OR & GE
    AG -- tool calls --> GW
    APR -- approved actions --> GW
    GW -- MCP JSON-RPC --> GS & CS
    GS --> GA
    CS --> GC
    GS & CS -- encrypted tokens --> DB
    API --> APR
    WF --> DB
    API -- OAuth code flow --> GO
```

The backend never passes Google tokens through MCP messages. Each MCP server process is told only **which user** it serves (`MCP_USER_ID`). It then loads that user's encrypted credentials from PostgreSQL, refreshing them if needed.

### Request lifecycle

A chat turn (`POST /api/conversations/{id}/messages`) works like this:

```mermaid
sequenceDiagram
    autonumber
    participant U as User (React)
    participant A as FastAPI
    participant G as LangGraph
    participant L as LLM router
    participant M as MCP gateway
    participant S as Gmail/Calendar MCP server
    participant D as PostgreSQL

    U->>A: POST /conversations/{id}/messages
    A->>D: save user message
    A->>G: invoke(graph, message)
    G->>D: load history, memory, pending request
    G->>L: recognize intent and entities (INTENT role)
    G->>G: enhance context, validate parameters
    alt parameters missing
        G-->>A: clarification question (pending request saved)
    else read operation
        G->>M: call_tool(search_emails / list_events ...)
        M->>S: tools/call
        S-->>M: structured result
        G->>L: summarize or compose (SUMMARY / TASK role), if needed
        G-->>A: result
    else write operation
        G->>D: create WorkflowExecution (awaiting_approval)
        G-->>A: preview and "approve or reject"
    end
    G->>D: persist assistant message, trace and memory
    A-->>U: ChatTurnOut {status, messages, workflow?}

    opt user approves
        U->>A: POST /workflows/{id}/decision {approve: true}
        A->>M: execute through MCP executor
        M->>S: send_email / create_event ...
        S-->>M: confirmed result
        A->>D: status succeeded/failed, append result message
    end
```

### LangGraph workflow

Defined in `backend/app/workflows/graph.py`. Every node is wrapped by a decorator that injects dependencies, records an entry in `trace`, and turns any exception into a classified `error`. An error sends the graph straight to `generate_response`, so the user always gets an answer.

```mermaid
flowchart TD
    START([START]) --> LC[load_context]
    LC --> SS[select_service]
    SS --> RI[recognize_intent]
    RI --> EC[enhance_context]
    EC --> VP[validate_parameters]

    VP -- "missing params / unknown / abandoned" --> GR
    VP -- gmail --> GMA[gmail_agent]
    VP -- calendar --> CAA[calendar_agent]

    GMA -- tool_calls --> MT[mcp_tools]
    CAA -- tool_calls --> MT
    MT -- "service = gmail" --> GMA
    MT -- "service = calendar" --> CAA

    GMA -- proposal --> RA[request_approval]
    CAA -- proposal --> RA
    GMA -- "result / clarify / error" --> GR
    CAA -- "result / clarify / error" --> GR
    RA --> GR[generate_response]

    GR --> PT[persist_turn]
    PT --> END([END])

    LC & SS & RI & EC -. error .-> GR
```

The agent and `mcp_tools` loop is capped at `MAX_AGENT_ITERATIONS = 8`. Each pass lets an agent ask for more data, for example searching for an event and then checking availability, before it returns a final outcome.

A turn ends with one of four statuses: `completed`, `needs_clarification`, `awaiting_approval` or `failed`.

### Agents

All agents live in `backend/app/agents/`. The operation agents follow one contract (`base.py`): each `step()` looks at the request and the tool results so far, and returns an `AgentOutcome` of kind `tool_calls`, `result`, `clarify`, `proposal` or `error`.

| Agent | File | Responsibility |
|---|---|---|
| Service Selector | `service_selector.py` | Weighted keyword and regex scoring picks `gmail` or `calendar` before intent recognition, then reconciles that choice with the recognized intent and any pending request. |
| Intent Recognition | `intent_agent.py` | Calls the LLM (INTENT role) for JSON intent and entities, validated by Pydantic (`schemas/intent.py`). Messages are limited to 2,000 characters. |
| Context Enhancement | `context_agent.py` | Merges history, session memory and pending entities. Resolves pronouns, candidate picks ("the second one") and relative dates (`dates.py`) in the user's IANA timezone. |
| Gmail agents | `gmail_agents.py` | One agent per intent: `search_emails`, `read_email`, `summarize_emails`, `reply_email`, `send_email`, `create_draft`. |
| Calendar agents | `calendar_agents.py` | One agent per intent: `get_events`, `check_availability`, `create_event`, `reschedule_event`, `cancel_event`. Events are matched safely (`integrations/calendar/matching.py`). |
| Summarization | `summarization_agent.py` | LLM (SUMMARY role) summaries of emails and threads. |
| Task automation | `task_agent.py` | LLM (TASK role) composition of email subjects and bodies. |

**Supported intents and required parameters** (enforced in `schemas/intent.py`, not by the LLM):

| Service | Intent | Required parameters | Needs approval |
|---|---|---|---|
| Gmail | `send_email` | recipient email, body | Yes |
| Gmail | `create_draft` | recipient email, body | Yes |
| Gmail | `reply_email` | email reference, body | Yes |
| Gmail | `search_emails` | query or person | No |
| Gmail | `read_email` | email reference | No |
| Gmail | `summarize_emails` | none | No |
| Calendar | `create_event` | title or person, date, time | Yes |
| Calendar | `reschedule_event` | event reference, new date or time | Yes |
| Calendar | `cancel_event` | event reference | Yes |
| Calendar | `get_events` | none | No |
| Calendar | `check_availability` | date | No |

### MCP layer

Implemented with the official Python MCP SDK in `backend/app/mcp/`.

| Server | Tools |
|---|---|
| `gmail_server.py` | `search_emails`, `read_email`, `get_email_thread`, `send_email`, `reply_email`, `create_draft` |
| `calendar_server.py` | `list_events`, `get_event`, `create_event`, `update_event`, `delete_event`, `check_availability` |

Each tool declares typed, bounded input schemas and read-only or destructive annotations.

- **`client.py` (`MCPGateway`)** is a synchronous facade over the async MCP client, driven by an anyio blocking portal. It connects to each server lazily, discovers tools with `tools/list`, **refuses to call a tool the server did not advertise**, applies a per-call timeout, and records `server`, `tool`, `ok` and `ms` for every call without recording arguments.
- **`runtime.py`** builds per-user service scopes. In stdio mode the child process reads `MCP_USER_ID` and loads that user's credentials itself.
- **`executors.py`** adapts approved workflow actions (`MCPGmailExecutor`, `MCPCalendarExecutor`) to MCP tool calls.
- **`errors.py`** maps tool errors to stable codes such as `google_reconnect_required` and `mcp_connection_failed`.

There are two transports, chosen with `MCP_TRANSPORT`:

| Value | Behaviour | Use for |
|---|---|---|
| `stdio` (default) | Each server runs as `python -m app.mcp.<server>` and speaks MCP over stdin/stdout | Normal operation, process isolation |
| `inprocess` | Same servers and protocol over an in-memory transport | Tests, single-process deployments |

### Multi-model LLM routing

`backend/app/services/llm/router.py` maps three **roles** to a provider and model. Each role falls back to the global `LLM_PROVIDER` and `LLM_MODEL` unless it is overridden.

| Role | Used for | Temperature | Override variables |
|---|---|---|---|
| `intent` | Intent and entity extraction | 0.1 | `INTENT_LLM_PROVIDER`, `INTENT_LLM_MODEL` |
| `summarization` | Email and thread summaries | 0.3 | `SUMMARY_LLM_PROVIDER`, `SUMMARY_LLM_MODEL` |
| `task_automation` | Email composition | 0.1 | `TASK_LLM_PROVIDER`, `TASK_LLM_MODEL` |

- Providers are `openrouter.py` (OpenAI-compatible API) and `gemini.py`. Both have retries and timeouts.
- `jsonutil.py` and `structured.py` pull and validate JSON from model output, including output from reasoning models like DeepSeek R1 that emit "thinking" text first.
- Settings validation rejects a role whose provider differs from `LLM_PROVIDER` but has no model set.
- `GET /api/llm/config` shows the effective routing without exposing secrets.

### Approval workflow

Consequential actions are stored as a `WorkflowExecution` and run only after an explicit decision.

```mermaid
stateDiagram-v2
    [*] --> awaiting_approval: agent returns a proposal
    awaiting_approval --> rejected: POST /decision {approve:false}<br/>or DELETE /workflows/{id}
    awaiting_approval --> running: POST /decision {approve:true}
    running --> succeeded: MCP tool confirms
    running --> failed: Google or MCP error
    rejected --> [*]
    succeeded --> [*]
    failed --> [*]
```

- **Idempotency:** chat proposals use the key `chat:<message_id>`, so a retried turn never creates a second proposal. Calendar creation uses the workflow id as an idempotency key, so a retried approval cannot create a duplicate event.
- **Verification:** send and draft results report whether Gmail confirmed the `SENT` label or the draft. Calendar writes re-read the event to confirm it. Results Google could not confirm are flagged to the user.
- **Connection check:** approval is refused with `google_reconnect_required` if the needed Google scope is no longer granted.
- Every decision and its outcome is added to the conversation as an assistant message, and conversation memory is updated (for example `last_event_id`).

### Conversation context and memory

There is no RAG or vector database. Context is structured and stored in PostgreSQL:

- **`messages`**: full history, plus a `metadata` JSON column holding the intent, entities, trace, LLM calls, MCP calls and any workflow reference.
- **`session_contexts`**: one row per conversation holding:
  - `entities`: long-lived memory such as `last_contact_email`, `last_event_id` and `last_sent_message_id`.
  - `pending_intent`: an unfinished request (intent, collected entities, awaited parameters, the question asked, and any event or email candidates).
  - `reference_date` and `timezone`.

When a turn needs clarification, its pending request is saved. On the next turn the Context Enhancement Agent continues that request instead of starting a new one. A failed turn keeps the existing pending request so nothing is lost. A message like "never mind" drops it.

### Data model

```mermaid
erDiagram
    users ||--o{ oauth_credentials : has
    users ||--o{ conversations : owns
    conversations ||--o{ messages : contains
    conversations ||--o| session_contexts : has
    conversations ||--o{ workflow_executions : produces

    users {
        uuid id PK
        string google_sub UK
        string email UK
        string name
        string timezone
        bool is_active
        datetime last_login_at
    }
    oauth_credentials {
        uuid id PK
        uuid user_id FK
        string provider
        text access_token_enc
        text refresh_token_enc
        datetime expires_at
        text scopes
        string status "active | revoked"
    }
    conversations {
        uuid id PK
        uuid user_id FK
        string title
        datetime updated_at
    }
    messages {
        uuid id PK
        uuid conversation_id FK
        string role "user | assistant | system"
        text content
        json metadata
    }
    session_contexts {
        uuid id PK
        uuid conversation_id FK, UK
        json entities
        json pending_intent
        date reference_date
        string timezone
    }
    workflow_executions {
        uuid id PK
        uuid user_id FK
        uuid conversation_id FK
        string status
        string service
        string intent
        string idempotency_key
        json request
        json result
        text error
    }
```

The schema is created by Alembic migration `backend/alembic/versions/0001_initial.py`. Every user-owned query is filtered by `user_id`.

### Security model

| Concern | Mitigation |
|---|---|
| OAuth CSRF | The signed `state` token must match an HTTP-only, `SameSite=Lax` nonce cookie (`oauth_nonce`, 10-minute lifetime, scoped to `/api/auth`). |
| Token leakage | Google access and refresh tokens are **Fernet-encrypted at rest** and never sent to the frontend or through MCP arguments. |
| Session token | After the callback, the app's own JWT is delivered in the **URL fragment** (`/auth/callback#token=...`), which browsers never send to servers or write to access logs. |
| Secrets in config | `JWT_SECRET` must be at least 32 characters, and `TOKEN_ENCRYPTION_KEY` must be a valid Fernet key. The app refuses to start otherwise. |
| Tenant isolation | Every conversation, workflow and credential lookup is filtered by the authenticated `user_id`. |
| Unsafe writes | All Gmail and Calendar writes go through the approval workflow. |
| Untrusted LLM output | Pydantic validation, email normalisation (a name in an email field is dropped), and deterministic checks for missing parameters. |
| Error leakage | Exception handlers (`api/errors.py`) return `{code, message}` only, with no stack traces or provider details. |
| Container | The backend image runs as a non-root `app` user. |

---

## Project structure

```text
agentic-workflow-assistant/
├── docker-compose.yml              # db (Postgres 16), backend, frontend
├── README.md
├── backend/
│   ├── Dockerfile                  # python:3.12-slim, non-root, runs migrations then uvicorn
│   ├── requirements.txt
│   ├── .env.example                # copy to .env (never commit .env)
│   ├── alembic.ini
│   ├── pytest.ini
│   ├── alembic/
│   │   ├── env.py
│   │   └── versions/0001_initial.py   # initial schema
│   └── app/
│       ├── main.py                 # FastAPI app factory, CORS, exception handlers
│       ├── api/
│       │   ├── router.py           # mounts all routers under /api
│       │   ├── deps.py             # current user, DB session, LLM router, MCP gateway factory
│       │   ├── errors.py           # exception -> {code, message} HTTP responses
│       │   └── routes/
│       │       ├── auth.py         # Google OAuth login/callback, /me, timezone, disconnect
│       │       ├── conversations.py# CRUD for conversations
│       │       ├── chat.py         # POST a message -> LangGraph turn
│       │       ├── workflows.py    # approval proposals and decisions
│       │       ├── gmail.py        # read-only Gmail endpoints
│       │       ├── calendar.py     # read-only Calendar endpoints
│       │       ├── intent.py       # intent analysis and LLM config
│       │       ├── mcp.py          # MCP tool discovery
│       │       └── health.py       # liveness and readiness
│       ├── agents/
│       │   ├── base.py             # AgentContext, AgentOutcome, ToolCall, Proposal contracts
│       │   ├── service_selector.py # Gmail vs Calendar routing
│       │   ├── intent_agent.py     # LLM intent and entity recognition
│       │   ├── context_agent.py    # memory, pronouns, candidates, pending requests
│       │   ├── dates.py            # relative-date resolution
│       │   ├── gmail_agents.py     # per-intent Gmail agents
│       │   ├── calendar_agents.py  # per-intent Calendar agents
│       │   ├── summarization_agent.py
│       │   └── task_agent.py       # email composition
│       ├── workflows/
│       │   ├── graph.py            # LangGraph nodes, routing, compilation
│       │   ├── state.py            # WorkflowState and WorkflowDeps
│       │   └── chat_service.py     # run_turn(), record_decision()
│       ├── mcp/
│       │   ├── gmail_server.py     # Gmail MCP server (6 tools)
│       │   ├── calendar_server.py  # Calendar MCP server (6 tools)
│       │   ├── client.py           # MCPGateway, stdio and in-process factories
│       │   ├── runtime.py          # per-user server scopes
│       │   ├── executors.py        # approved action -> MCP tool call
│       │   └── errors.py
│       ├── integrations/
│       │   ├── http.py             # shared Google HTTP client and error mapping
│       │   ├── errors.py           # GoogleAuthError, GoogleNotFoundError, ...
│       │   ├── gmail/              # client.py (REST), service.py, mime.py (MIME build/parse)
│       │   └── calendar/           # client.py, service.py, matching.py, timeutil.py, models.py
│       ├── services/
│       │   ├── auth_service.py     # user upsert, credential storage, connection status
│       │   ├── google_oauth.py     # auth URL, code exchange, ID-token verification
│       │   ├── token_service.py    # decrypt and refresh access tokens
│       │   ├── workflow_service.py # propose, reject, execute
│       │   └── llm/
│       │       ├── router.py       # role -> provider/model
│       │       ├── base.py         # provider interface and LLM errors
│       │       ├── openrouter.py
│       │       ├── gemini.py
│       │       ├── prompts.py      # system prompts
│       │       ├── structured.py   # validated structured output
│       │       └── jsonutil.py     # robust JSON extraction
│       ├── core/
│       │   ├── config.py           # Pydantic Settings, validated at startup
│       │   ├── security.py         # JWT, OAuth state, Fernet helpers
│       │   └── validation.py       # email and time normalisation
│       ├── database/
│       │   ├── base.py             # declarative base, portable JSON type
│       │   ├── models.py           # ORM models
│       │   └── session.py          # engine and get_db dependency
│       ├── schemas/                # Pydantic request/response models
│       │   ├── chat.py
│       │   ├── conversation.py
│       │   ├── intent.py           # intents, entities, required parameters
│       │   ├── user.py
│       │   └── workflow.py
│       └── tests/                  # pytest suite (22 modules, SQLite and fakes)
└── frontend/
    ├── Dockerfile                  # node:22-alpine, Vite dev server
    ├── package.json
    ├── index.html
    └── src/
        ├── main.jsx                # login, OAuth callback, workspace, approvals
        └── styles.css
```

---

## Local development setup

### Prerequisites

- Python 3.12 or newer
- PostgreSQL 16 (or use the Compose `db` service)
- Node.js 20+ and npm
- A Google Cloud project with OAuth credentials (for live Google features)
- An OpenRouter or Gemini API key (for live LLM calls)

### 1. Backend environment

```powershell
cd agentic-workflow-assistant
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
Copy-Item backend\.env.example backend\.env   # then fill in secrets
```

On macOS or Linux, use `source .venv/bin/activate` and `cp`.

### 2. Database

```powershell
docker compose up -d db          # or point DATABASE_URL at your own PostgreSQL
cd backend
python -m alembic upgrade head
```

The default URL is `postgresql+psycopg://assistant:assistant@localhost:5432/assistant`. Only run migrations against a database you own.

### Google OAuth configuration

1. In Google Cloud Console, create or select a project.
2. Enable the **Gmail API** and the **Google Calendar API**.
3. Configure the OAuth consent screen. While the app is in testing mode, add your account as a test user.
4. Create an **OAuth client ID** of type *Web application*.
5. Add the authorized redirect URI `http://localhost:8000/api/auth/google/callback`.
6. Put the client ID and secret into `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

The app requests these scopes: `openid`, `userinfo.email`, `userinfo.profile`, `gmail.modify` (read, send, drafts, threads) and `calendar` (events and free/busy).

### 3. Run the backend

```powershell
cd backend
python -m uvicorn app.main:app --reload
```

Check it: http://localhost:8000/api/health (liveness) and http://localhost:8000/api/health/ready (database).

### 4. Run the frontend

```powershell
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. The frontend calls `VITE_API_URL`, which defaults to `http://localhost:8000/api`.

---

## Configuration reference

All settings are read from `backend/.env` by `app/core/config.py` and validated at startup.

| Variable | Default | Required | Description |
|---|---|---|---|
| `APP_ENV` | `development` | No | `production` turns on secure cookies. |
| `DATABASE_URL` | local `assistant` DB | Yes | SQLAlchemy URL (`postgresql+psycopg://...`). |
| `JWT_SECRET` | none | **Yes** | At least 32 characters. Signs app JWTs and OAuth state. |
| `JWT_ALGORITHM` | `HS256` | No | JWT algorithm. |
| `JWT_EXPIRE_MINUTES` | `480` | No | Session lifetime. |
| `TOKEN_ENCRYPTION_KEY` | none | **Yes** | Fernet key used to encrypt Google tokens. |
| `GOOGLE_CLIENT_ID` | none | For OAuth | OAuth web client ID. |
| `GOOGLE_CLIENT_SECRET` | none | For OAuth | OAuth web client secret. |
| `GOOGLE_REDIRECT_URI` | `http://localhost:8000/api/auth/google/callback` | No | Must match the URI registered in Google Cloud. |
| `FRONTEND_URL` | `http://localhost:5173` | No | Where the OAuth callback redirects to. |
| `CORS_ORIGINS` | `http://localhost:5173` | No | Comma-separated allowed origins. |
| `OPENROUTER_API_KEY` | none | For OpenRouter | OpenRouter key. |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | No | |
| `OPENROUTER_JSON_MODE` | `false` | No | Sends `response_format`. Leave off for DeepSeek R1. |
| `GEMINI_API_KEY` | none | For Gemini | Gemini key. |
| `GEMINI_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta` | No | |
| `LLM_PROVIDER` | `openrouter` | No | `openrouter` or `gemini`. |
| `LLM_MODEL` | `deepseek/deepseek-r1-0528` | No | Default model for every role. |
| `INTENT_LLM_PROVIDER` / `_MODEL` | blank | No | Intent-role override. |
| `SUMMARY_LLM_PROVIDER` / `_MODEL` | blank | No | Summarization-role override. |
| `TASK_LLM_PROVIDER` / `_MODEL` | blank | No | Email-composition-role override. |
| `LLM_TIMEOUT_SECONDS` | `120` | No | Provider HTTP timeout. |
| `LLM_MAX_RETRIES` | `2` | No | Retries after the first attempt. |
| `LLM_MAX_TOKENS` | `4096` | No | Includes reasoning tokens for R1-style models. |
| `MCP_TRANSPORT` | `stdio` | No | `stdio` or `inprocess`. |
| `MCP_TIMEOUT_SECONDS` | `60` | No | Per-tool-call timeout. |

Frontend: `VITE_API_URL` (default `http://localhost:8000/api`).

**Example: Gemini for intent recognition, DeepSeek for everything else**

```env
LLM_PROVIDER=openrouter
LLM_MODEL=deepseek/deepseek-r1-0528
INTENT_LLM_PROVIDER=gemini
INTENT_LLM_MODEL=gemini-2.0-flash
```

---

## API reference

All routes are prefixed with `/api`. Everything except the health and OAuth endpoints requires `Authorization: Bearer <jwt>`. Full schemas are at `/docs` (Swagger) and `/redoc`.

Errors use one shape:

```json
{ "detail": { "code": "google_reconnect_required", "message": "Reconnect the required Google service." } }
```

### Authentication

| Method | Path | Description |
|---|---|---|
| `GET` | `/auth/google/login` | Redirects to Google consent and sets the CSRF nonce cookie. |
| `GET` | `/auth/google/callback` | Exchanges the code, stores encrypted tokens, redirects to `FRONTEND_URL/auth/callback#token=...`. |
| `GET` | `/auth/me` | Profile plus Gmail and Calendar connection status. |
| `PATCH` | `/auth/me/timezone` | Sets the IANA timezone, for example `{"timezone": "Asia/Kolkata"}`. |
| `POST` | `/auth/logout` | Client-side logout (JWTs are stateless). |
| `DELETE` | `/auth/google/connection` | Revokes and deletes the stored Google credentials. |

### Conversations and chat

| Method | Path | Description |
|---|---|---|
| `POST` | `/conversations` | Create a conversation. |
| `GET` | `/conversations` | List your conversations. |
| `GET` | `/conversations/{id}` | A conversation with its messages. |
| `DELETE` | `/conversations/{id}` | Delete a conversation. |
| `POST` | `/conversations/{id}/messages` | Run one turn through LangGraph. Body: `{"content": "..."}`. |

Example turn response:

```json
{
  "status": "awaiting_approval",
  "user_message": { "id": "...", "role": "user", "content": "Email priya@example.com that the demo moved to Friday" },
  "assistant_message": { "id": "...", "role": "assistant", "content": "To: priya@example.com\nSubject: ...\n\nApprove to go ahead, or reject to cancel. Nothing happens until you approve.", "meta": { "trace": [ ... ] } },
  "workflow": { "id": "...", "status": "awaiting_approval", "service": "gmail", "intent": "send_email" }
}
```

### Workflows (approvals)

| Method | Path | Description |
|---|---|---|
| `POST` | `/workflows` | Create a proposal directly. |
| `GET` | `/workflows` | List your workflows, newest first. |
| `GET` | `/workflows/{id}` | Get one workflow. |
| `POST` | `/workflows/{id}/decision` | `{"approve": true}` runs the action through MCP; `{"approve": false}` rejects it. |
| `DELETE` | `/workflows/{id}` | Reject a pending workflow. |

### Read-only Gmail and Calendar

| Method | Path | Description |
|---|---|---|
| `GET` | `/gmail/messages?q=...` | Search Gmail. |
| `GET` | `/gmail/messages/{id}` | Read a message. |
| `GET` | `/gmail/threads/{id}` | Read a thread. |
| `GET` | `/calendar/events?days=7&q=...` | List upcoming events. |
| `GET` | `/calendar/next` | The next event. |
| `GET` | `/calendar/availability?start=...&end=...` | Free/busy and free slots. |

### Diagnostics

| Method | Path | Description |
|---|---|---|
| `POST` | `/intent/analyze` | Classify a message without taking any action. |
| `GET` | `/llm/config` | Effective provider and model per role (no secrets). |
| `GET` | `/mcp/tools` | Tools discovered from both MCP servers. |
| `GET` | `/health` | Liveness. |
| `GET` | `/health/ready` | Readiness (database connectivity). |

---

## Example conversation

```text
You:        Schedule a meeting with Arjun tomorrow
Assistant:  What time?                                   ← missing parameter; request kept pending
You:        4pm, invite arjun@example.com
Assistant:  Create "Meeting with Arjun", Sat 28 Nov, 16:00–17:00, attendee arjun@example.com.
            Approve to go ahead, or reject to cancel.    ← awaiting_approval
[Approve]
Assistant:  Created "Meeting with Arjun" starting 2026-11-28T16:00:00+05:30.
You:        Actually move it to 5                        ← "it" resolved from memory (last_event_id)
Assistant:  Move "Meeting with Arjun" to Sat 28 Nov, 17:00? Approve or reject.
```

---

## Testing

The backend suite runs against isolated SQLite databases, with fake Google and LLM providers. It needs no network access or credentials.

```powershell
cd backend
python -m pytest -q                      # full suite
python -m pytest app/tests/test_mcp.py   # a single module
python -m pytest --cov=app               # with coverage
python -m compileall -q app              # syntax check
```

| Area | Test modules |
|---|---|
| LangGraph and chat | `test_chat_workflow.py`, `test_workflows.py`, `test_conversations.py` |
| Agents | `test_intent.py`, `test_context_agent.py`, `test_service_selector.py`, `test_specialized_agents.py` |
| MCP | `test_mcp.py` (protocol, discovery, unknown-tool rejection, approval execution) |
| LLM | `test_llm.py`, `test_model_roles.py`, `test_jsonutil.py` |
| Google | `test_gmail_service.py`, `test_calendar_service.py`, `test_google_http.py`, `test_google_routes.py`, `test_mime.py`, `test_matching.py`, `test_timeutil.py` |
| Auth and security | `test_auth_flow.py`, `test_security.py`, `test_token_service.py`, `test_validation.py` |

Shared fixtures are in `conftest.py`, and fake providers are in `fakes.py`. The frontend has no test script yet. Use `npm run build` to check that it compiles.

---

## Extending the assistant

### Add a new intent (for example `archive_email`)

1. **Schema:** add the intent to `GMAIL_INTENTS` and `Intent` in `schemas/intent.py`, then list its required parameters in `REQUIREMENTS` and their questions in `QUESTIONS`.
2. **Prompt:** describe the intent and its entities in `services/llm/prompts.py` so the Intent Recognition Agent can produce it.
3. **MCP tool:** add a `@server.tool(...)` to `mcp/gmail_server.py` with typed arguments and the right read-only or destructive annotation. Implement it in `integrations/gmail/service.py`.
4. **Agent:** subclass `OperationAgent` in `agents/gmail_agents.py`, implement `step()` to return `ToolCall`s, then a `result` or a `Proposal`, and register it in `build_gmail_agents()`.
5. **Approval (writes only):** add the intent to `GMAIL_INTENTS` in `services/workflow_service.py`, handle it in `execute()`, add a method to `MCPGmailExecutor`, and add a result message in `chat_service._result_text()`.
6. **Tests:** cover the schema requirements, agent steps and MCP tool, using the existing fakes.

### Add an LLM provider

Implement the `LLMProvider` interface from `services/llm/base.py`, register it in `LLMRouter._provider()`, and allow its name in the provider validator in `core/config.py`.

### Schema changes

```powershell
cd backend
python -m alembic revision --autogenerate -m "describe change"
python -m alembic upgrade head
```

---

## Troubleshooting and error codes

| Code or symptom | Meaning | Fix |
|---|---|---|
| App fails on startup with a validation error | `JWT_SECRET` is too short or `TOKEN_ENCRYPTION_KEY` is not a valid Fernet key | Regenerate both with the commands in [Quick start](#quick-start-docker). |
| `/login?error=not_configured` | Google client ID, secret or redirect URI is missing | Set them in `backend/.env`. |
| `/login?error=invalid_state` | The OAuth nonce cookie is missing or doesn't match | Start again from `/api/auth/google/login` in the same browser, and don't mix `localhost` with `127.0.0.1`. |
| `redirect_uri_mismatch` (from Google) | The redirect URI isn't registered | Add exactly `GOOGLE_REDIRECT_URI` to the OAuth client. |
| `google_reconnect_required` | Token revoked or expired, or a scope is missing | Sign in with Google again. |
| `google_api_not_enabled` / `service_disabled` | Gmail or Calendar API is disabled in the GCP project | Enable it in Google Cloud Console. |
| `google_rate_limited` | Google quota was hit | Wait and retry. |
| `llm_not_configured` | No API key for the configured provider | Set `OPENROUTER_API_KEY` or `GEMINI_API_KEY`, then check `/api/llm/config`. |
| `llm_unavailable` / `llm_invalid_response` | Provider error or unusable output | Retry, raise `LLM_TIMEOUT_SECONDS` or `LLM_MAX_TOKENS`, or switch the role to another model. |
| `mcp_connection_failed` | An MCP subprocess couldn't start or respond | Check backend logs, and set `MCP_TRANSPORT=inprocess` to isolate the problem. |
| `/api/health/ready` fails | The database is unreachable | Check `DATABASE_URL` and that PostgreSQL is running. |
| Workflow decision returns `409` | The workflow is no longer `awaiting_approval` | It was already approved or rejected. Reload the workflow. |

---

## Known limitations

- **Stateless logout:** JWTs are not revoked on the server. Logout just discards the token on the client, and tokens expire after `JWT_EXPIRE_MINUTES`.
- **Token storage:** the frontend keeps the JWT in `localStorage`. An HTTP-only cookie session would be harder to steal through XSS.
- **All-day events** cannot be rescheduled by the Calendar agent.
- **Calendar listing** is bounded by a look-ahead window (1 to 90 days) and result limits.
- **Single calendar:** only the user's primary calendar is used.
- **Frontend dependencies** are pinned to `latest` in `package.json`. Pin versions for reproducible builds.
- **Development server in Docker:** the frontend container runs the Vite dev server. For production, serve `npm run build` output from a static server.
- **Mocked tests:** the test suite uses fakes, so it does not prove the live Google or LLM integrations work. Those need real credentials and a consenting account.
- **Approved writes are real:** approved actions really send email and change calendars. Use a test Google account while developing.
