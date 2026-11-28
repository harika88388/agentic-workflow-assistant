# Agentic Workflow Assistant

A conversational assistant that turns plain-English requests into **safe, verified Gmail and Google Calendar actions**.

You sign in with Google and type something like *"did Priya email me about the demo?"*, *"email arjun@example.com that the review moved to Friday"* or *"move my sync with Arjun to 3pm tomorrow"*. A LangGraph pipeline of specialized agents works out what you mean and asks for anything missing. It reads your mailbox and calendar through **Model Context Protocol (MCP)** servers and answers from the real email bodies. Anything that would send mail or change your calendar waits in an **approval queue** and runs only after you click Approve.

**Live deployment:** [agentic-workflow-assistant.vercel.app](https://agentic-workflow-assistant.vercel.app)

| Layer | Technology |
|---|---|
| API | Python 3.12, FastAPI, Pydantic 2, PyJWT |
| Orchestration | LangGraph state graph with conditional routing |
| Tools | Official Python MCP SDK, with separate Gmail and Calendar MCP servers |
| LLMs | OpenRouter (DeepSeek R1 by default) and Google Gemini, routed per role |
| Persistence | PostgreSQL 16, SQLAlchemy 2.0, Alembic |
| Security | Google OAuth 2.0 (Authorization Code flow), Fernet-encrypted tokens, JWT sessions |
| Frontend | React, Vite, Lucide icons, streamed (NDJSON) responses |
| Runtime | Docker Compose locally; Vercel Services and Neon Postgres in production |

---

## Table of contents

1. [Features](#features)
2. [Quick start with Docker](#quick-start-with-docker)
3. [Architecture](#architecture)
   - [Design principles](#design-principles)
   - [System overview](#system-overview)
   - [Request lifecycle](#request-lifecycle)
   - [LangGraph workflow](#langgraph-workflow)
   - [Agents](#agents)
   - [Grounded email answers](#grounded-email-answers)
   - [MCP layer](#mcp-layer)
   - [Multi-model LLM routing](#multi-model-llm-routing)
   - [Approval workflow](#approval-workflow)
   - [Conversation context and memory](#conversation-context-and-memory)
   - [Streaming and recovery](#streaming-and-recovery)
   - [Observability](#observability)
   - [Data model](#data-model)
   - [Security model](#security-model)
4. [Frontend](#frontend)
5. [Project structure](#project-structure)
6. [Local development](#local-development)
7. [Deploying to Vercel](#deploying-to-vercel)
8. [Configuration reference](#configuration-reference)
9. [API reference](#api-reference)
10. [Example conversations](#example-conversations)
11. [Testing](#testing)
12. [Extending the assistant](#extending-the-assistant)
13. [Troubleshooting and error codes](#troubleshooting-and-error-codes)
14. [Known limitations](#known-limitations)

---

## Features

- **Natural-language Gmail.** Search, read, summarize, reply to, draft and send email. Questions such as "when is the meeting Arjun mentioned?" are answered from the full email bodies, not just subjects and snippets.
- **Natural-language Google Calendar.** List events, check availability, and create, reschedule or cancel events.
- **Email to calendar.** When an email mentions an upcoming meeting, the assistant offers to add it to your calendar, pre-filled from the email.
- **Multi-agent orchestration.** A Service Selector, Intent Recognition, Context Enhancement, Email Understanding and eleven per-operation Gmail and Calendar agents, wired together with LangGraph.
- **MCP tool layer.** Gmail and Calendar are exposed as two MCP servers with 12 tools in total. Agents never call Google directly.
- **Human-in-the-loop safety.** Every write becomes an `awaiting_approval` workflow. Nothing is sent, created, moved or deleted until you approve it.
- **Multi-turn context.** The assistant resolves pronouns ("him", "that meeting"), choices ("the second one") and relative dates ("next Tuesday") in your timezone, and picks up half-finished requests across turns.
- **Deterministic validation.** The LLM extracts entities, but plain code decides which parameters are missing, so a model can't skip a required field or invent an email address.
- **Role-based model routing.** Intent recognition, email understanding and summarization, and email composition can each use a different provider and model.
- **Honest reporting.** An action is reported as done only after an MCP tool has confirmed it with Google. Errors are classified and explained in plain language.
- **Live progress and safe recovery.** The UI streams each workflow step as it runs. If the connection drops, the UI recovers the real outcome from the server instead of resending, so an action can't run twice.
- **Full traceability.** Every turn records the node trace, model calls and MCP calls in an Activity view. Logs are correlated by request ID, and secrets are redacted.

---

## Quick start with Docker

Prerequisites: Docker Desktop. For live use you also need Google OAuth credentials ([setup](#google-oauth-configuration)) and an OpenRouter or Gemini API key.

```powershell
# 1. Create the backend env file
Copy-Item backend\.env.example backend\.env

# 2. Generate the two required secrets and paste them into backend\.env
python -c "import secrets; print(secrets.token_urlsafe(48))"                               # JWT_SECRET
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # TOKEN_ENCRYPTION_KEY

# 3. Fill in GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and OPENROUTER_API_KEY (or GEMINI_API_KEY)

# 4. Start everything. The backend applies Alembic migrations on boot.
docker compose up --build
```

On macOS or Linux, use `cp` instead of `Copy-Item`.

| Service | URL |
|---|---|
| Frontend | http://localhost:5173 |
| Backend API | http://localhost:8000/api |
| Interactive API docs (Swagger) | http://localhost:8000/docs |
| Readiness self-check | http://localhost:8000/api/health/ready |
| PostgreSQL | `localhost:5432` (user, password and database are all `assistant`) |

Compose starts the services in dependency order: `db`, then `backend` once Postgres is healthy, then `frontend` once `/api/health/ready` answers.

---

## Architecture

### Design principles

These rules shape the whole codebase:

1. **The LLM proposes, code decides.** Models classify intent, extract entities, compose text and read emails. Deterministic code checks required parameters, validates addresses and times, matches events and enforces approval.
2. **No write without approval.** Agents can only *propose* a write. A write runs only through `POST /workflows/{id}/decision`.
3. **Nothing is "done" until Google confirms it.** Writes are re-read or label-checked, and unconfirmed results are reported as such.
4. **Tokens never leave the backend.** Google tokens are encrypted at rest. They never reach the browser, MCP messages, logs or execution history.
5. **Every failure produces an answer.** Any node failure is caught, classified and routed to a response, so the user never sees a silent hang or a fake success.

### System overview

```mermaid
flowchart LR
    subgraph Browser
        UI[React + Vite workspace]
    end

    subgraph Backend["FastAPI backend"]
        API[REST + NDJSON streaming routes<br/>JWT auth, request IDs]
        WF[LangGraph workflow]
        AG[Specialized agents]
        LLM[LLM router]
        GW[MCP gateway]
        APR[Approval service]
        HIST[Execution history]
    end

    subgraph MCP["MCP servers (stdio subprocesses or in-process)"]
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
    GS & CS -- load encrypted tokens --> DB
    API --> APR
    WF --> DB
    HIST --> DB
    API -- OAuth code flow --> GO
```

Google tokens are never passed through MCP messages. Each MCP server process is told only **which user** it serves (the `MCP_USER_ID` environment variable). It then loads that user's encrypted credentials from PostgreSQL and refreshes them when needed.

### Request lifecycle

The web UI sends each chat turn to `POST /api/conversations/{id}/messages/stream`. The non-streaming `POST /api/conversations/{id}/messages` endpoint runs the same turn and returns the result in a single response.

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

    U->>A: POST /conversations/{id}/messages/stream
    A->>D: save user message, start agent_runs row
    A-->>U: {"type":"started", execution_id}
    A->>G: invoke(graph, message)
    G->>D: load history, memory, pending request
    G->>L: recognize intent and entities (intent role)
    G->>G: enhance context, validate parameters
    G-->>U: progress events per node
    alt parameters missing
        G-->>A: clarification question (pending request saved)
    else read operation
        G->>M: call_tool(search_emails / read_email / list_events ...)
        M->>S: tools/call
        S-->>M: structured result
        G->>L: understand, summarize or compose, if needed
    else write operation
        G->>D: create workflow_executions row (awaiting_approval)
    end
    G->>D: save assistant message, trace and memory
    A->>D: finish agent_runs row
    A-->>U: {"type":"result", turn}

    opt user approves
        U->>A: POST /workflows/{id}/decision {approve: true}
        A->>M: run the action through an MCP executor
        M->>S: send_email / create_event ...
        S-->>M: confirmed result
        A->>D: status succeeded or failed, add result message, update agent_runs
    end
```

### LangGraph workflow

The graph is defined in `backend/app/workflows/graph.py`. A `node` decorator wraps every node: it injects dependencies, records a `trace` entry with status and duration, emits a progress event for the UI, and turns any exception into a classified `error`. When a node fails, the graph goes straight to `generate_response`, so the user always gets an answer.

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

| Node | What it does |
|---|---|
| `load_context` | Loads history, session memory and any pending (unfinished) request. |
| `select_service` | Scores the message for Gmail or Calendar signals to give intent recognition a hint. |
| `recognize_intent` | Extracts intent and entities, using a deterministic shortcut for clear email-reading requests and the LLM otherwise. |
| `enhance_context` | Resolves pronouns, candidate picks and relative dates, continues pending requests, and reconciles the final service. |
| `validate_parameters` | Recomputes missing parameters from `REQUIREMENTS`; asks a question if anything is missing. |
| `gmail_agent` / `calendar_agent` | Runs the per-intent operation agent for one step. |
| `mcp_tools` | Executes the tool calls the agent asked for, then loops back to the agent. |
| `request_approval` | Stores the proposed write as an `awaiting_approval` workflow. |
| `generate_response` | Turns the outcome or error into user-facing text. |
| `persist_turn` | Saves the assistant message, its metadata, memory updates and the pending request. |

The agent and `mcp_tools` loop is capped at `MAX_AGENT_ITERATIONS = 8`. That lets an agent gather data in several rounds, for example searching for emails and then reading the top matches, before it returns a final outcome.

A turn ends with one of four statuses: `completed`, `needs_clarification`, `awaiting_approval` or `failed`.

### Agents

All agents live in `backend/app/agents/`. Operation agents share one contract (`base.py`). Each `step()` receives an `AgentContext` (intent, entities, memory, tool results so far) and returns an `AgentOutcome` of kind `tool_calls`, `result`, `clarify`, `proposal` or `error`.

| Agent | File | Responsibility |
|---|---|---|
| Service Selector | `service_selector.py` | Weighted keyword and regex scoring picks `gmail` or `calendar` before intent recognition, then reconciles that choice with the recognized intent and any pending request. |
| Intent Recognition | `intent_agent.py` | Calls the LLM (`intent` role) for JSON intent and entities, validated by Pydantic (`schemas/intent.py`). Clear email-reading questions are matched by regex without an LLM call. Messages are limited to 2,000 characters. |
| Context Enhancement | `context_agent.py`, `dates.py` | Merges history, session memory and pending entities. Resolves pronouns, candidate picks and relative dates in the user's IANA timezone. Detects "never mind" and drops the pending request. |
| Gmail operation agents | `gmail_agents.py` | One agent per intent: `search_emails`, `read_email`, `summarize_emails`, `reply_email`, `send_email`, `create_draft`. |
| Calendar operation agents | `calendar_agents.py` | One agent per intent: `get_events`, `check_availability`, `create_event`, `reschedule_event`, `cancel_event`. Events are matched safely (`integrations/calendar/matching.py`), and an ambiguous match produces a list of candidates. |
| Email Understanding | `email_understanding_agent.py` | Structured analysis of one email, or of a question across up to five matching emails, using the `email_understanding` role. Output is checked against the email text (see below). |
| Summarization | `summarization_agent.py` | Summaries, key points and action items for emails and threads (`summarization` role). |
| Task Automation | `task_agent.py` | Writes email subjects and bodies (`task_automation` role), with a deterministic fallback subject. |

**Supported intents.** Required parameters are enforced in `schemas/intent.py`, not by the LLM.

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

### Grounded email answers

Email content is untrusted third-party text, and models can hallucinate. The search agent handles both concerns like this:

1. Search Gmail with the derived query (up to 10 matches). If nothing matches, retry with progressively broader queries.
2. Read the **full bodies** of the top five matches through the `read_email` MCP tool.
3. Ask the `email_understanding` model to answer the user's question across those emails, capped at 8,000 characters per email and 32,000 in total. Its answer is structured: findings per email, relevance, evidence and meeting details.
4. **Ground the answer.** Evidence, dates, times and meeting links the model claims are checked against the email text. Anything not found in the email is dropped or reported as "not stated in the email".
5. If exactly one relevant email describes an upcoming meeting that isn't cancelled, offer to create a calendar event. Relative dates such as "tomorrow" are resolved against the date the email was sent. The offer becomes a pending `create_event` request, so a "yes" continues it, and the event itself still needs approval.

If the model is unavailable, the agent lists the matching emails instead of failing the turn.

### MCP layer

The MCP layer in `backend/app/mcp/` is built on the official Python MCP SDK.

| Server | Tools |
|---|---|
| `gmail_server.py` | `search_emails`, `read_email`, `get_email_thread`, `send_email`, `reply_email`, `create_draft` |
| `calendar_server.py` | `list_events`, `get_event`, `create_event`, `update_event`, `delete_event`, `check_availability` |

Every tool has typed, bounded input schemas (for example, `list_events` accepts `days` from 1 to 90) and is annotated as read-only, write or destructive.

- **`client.py` (`MCPGateway`)** is a synchronous facade over the async MCP client, driven by an anyio blocking portal. It connects to each server lazily, discovers tools with `tools/list`, **refuses to call any tool the server did not advertise**, applies a per-call timeout, and records `server`, `tool`, `ok` and `ms` for every call. Arguments are never recorded.
- **`runtime.py`** builds per-user service scopes. In stdio mode, the child process reads `MCP_USER_ID` and loads that user's credentials itself, and it logs to stderr because stdout carries the protocol.
- **`executors.py`** maps approved workflow actions to MCP tool calls (`MCPGmailExecutor`, `MCPCalendarExecutor`).
- **`errors.py`** maps Google and integration errors to stable codes such as `google_reconnect_required`, `not_found` and `verification_failed`.

`MCP_TRANSPORT` selects one of two transports:

| Value | Behaviour | Use for |
|---|---|---|
| `stdio` (default) | Each server runs as `python -m app.mcp.<server>` and speaks MCP over stdin and stdout | Local and Docker use, with process isolation |
| `inprocess` | The same servers and protocol over an in-memory transport | Tests and serverless deployments (Vercel) |

### Multi-model LLM routing

`backend/app/services/llm/router.py` maps four **roles** to a provider and model. Each role uses the global `LLM_PROVIDER` and `LLM_MODEL` unless overridden.

| Role | Used for | Temperature | Override variables |
|---|---|---|---|
| `intent` | Intent and entity extraction | 0.1 | `INTENT_LLM_PROVIDER`, `INTENT_LLM_MODEL` |
| `summarization` | Email and thread summaries | 0.3 | `SUMMARY_LLM_PROVIDER`, `SUMMARY_LLM_MODEL` |
| `email_understanding` | Reading emails and answering questions about them | 0.2 | Shares the `SUMMARY_*` overrides |
| `task_automation` | Email composition | 0.1 | `TASK_LLM_PROVIDER`, `TASK_LLM_MODEL` |

- The providers are `openrouter.py` (OpenAI-compatible API) and `gemini.py`. Both retry on 429, 5xx and timeouts with exponential backoff, and stop immediately on 401 or 403.
- **Per-turn time budget.** `LLM_TURN_BUDGET_SECONDS` (default 240) caps the total LLM time in one turn, including retries. A turn that runs out of budget fails cleanly with a message, well before the 300-second serverless limit kills the function.
- `jsonutil.py` and `structured.py` extract and validate JSON from model output. That includes reasoning models such as DeepSeek R1, which write "thinking" text before the answer.
- Settings validation rejects any role whose provider differs from `LLM_PROVIDER` when that role has no model set.
- `GET /api/llm/config` shows the effective routing per role without exposing any secrets.

### Approval workflow

Consequential actions are stored as `workflow_executions` rows and run only after an explicit decision.

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

- **Idempotency.** Chat proposals use the key `chat:<message_id>`, so retrying a turn never creates a second proposal. For event creation, the Google event ID is derived from the workflow ID, so retrying an approval can't create a duplicate event.
- **Verification.** Send and draft results report whether Gmail confirmed the `SENT` label or the draft. Calendar writes re-read the event, and deletes confirm it is gone. Results Google couldn't confirm are flagged to the user.
- **Connection check.** Approval is refused with `google_reconnect_required` if the Google scope the action needs is no longer granted.
- **Recorded outcome.** Each decision and its result are added to the conversation as an assistant message and attached to the turn's `agent_runs` row. Conversation memory is updated too, for example `last_event_id` or `last_sent_message_id`.

### Conversation context and memory

There is no RAG pipeline or vector database. Context is structured and stored in PostgreSQL:

- **`messages`** holds the full history. Its `metadata` JSON column stores the intent, entities, trace, model calls, MCP calls, request ID and any workflow reference.
- **`session_contexts`** has one row per conversation with:
  - `entities`: long-lived memory such as `last_contact_email`, `last_event_id`, `last_sent_message_id` and the most recently listed emails.
  - `pending_intent`: an unfinished request, with its intent, collected entities, awaited parameters, the question asked, and any event or email candidates.
  - `reference_date` and `timezone`.

When a turn needs clarification, its pending request is saved, and the next turn continues it instead of starting over. A failed turn keeps the existing pending request so nothing is lost. A message like "never mind" drops it. The browser syncs its IANA timezone to the account on load (`PATCH /auth/me/timezone`), so "tomorrow at 3" means your local time.

### Streaming and recovery

The streaming endpoint returns `application/x-ndjson`, with one JSON event per line:

| Event | Payload |
|---|---|
| `started` | `request_id`, `execution_id`, `user_message_id` |
| `progress` | LangGraph `node`, `status` (`running`, `ok` or `error`), human-readable `label`, `ms` |
| `tool` | MCP `server` and `tool`, `status`, `ms` |
| `heartbeat` | Sent every 5 seconds while the turn runs |
| `result` | The final turn: status, user message, assistant message, workflow, execution ID |
| `error` | `code`, `message` and `request_id` |

The turn runs in a worker thread with its own database session. If the client disconnects, the server **keeps running the turn and persists it**, holding the request open for up to 270 seconds. On the client, `api.js` treats 20 seconds without data as a stalled connection. After a stall, timeout or dropped stream, the UI polls `GET /executions/{id}` (or `GET /executions?request_id=...` if the `started` event never arrived) and loads the saved conversation. It **never resubmits automatically**. Runs left in `running` past 330 seconds (the function limit plus a margin) are reported as `failed` with code `timed_out`.

### Observability

**Application logs** (`core/log_config.py`):

- One stdout handler. Lines are JSON in production (or whenever `VERCEL` is set) and readable `key=value` text locally.
- Each line includes a UTC timestamp, level, logger and message, plus the request-scoped context: `request_id`, `user_id`, `conversation_id`, `execution_id` and `workflow_id`.
- `RequestContextMiddleware` accepts a well-formed `X-Request-ID` from the client, or generates one, and returns it in the response header. The frontend sends one with every request, so a browser console line can be matched to backend logs.
- The formatter **redacts** bearer tokens, JWTs, Google access and refresh tokens, OAuth client secrets, API keys, Fernet keys, credentials inside URLs, OAuth query parameters and sensitive dictionary keys. It also masks the local part of email addresses. Email bodies are never logged.
- Unhandled exceptions are logged with a stack trace and returned as a JSON 500 that includes the `request_id`.

**Execution history** (`services/execution_history.py`, table `agent_runs`): each assistant turn gets one row holding the intent, service, agent, workflow steps, MCP tool calls (with their phase, workflow or approval), model calls with tokens and latency, approval status, action status, errors, duration and request ID. It stores metadata only, never email content or credentials. The history is best-effort: a failure to record it is logged and never fails your request. The Activity tab shows it, and `/api/executions` serves it, scoped to the signed-in user.

### Data model

```mermaid
erDiagram
    users ||--o{ oauth_credentials : has
    users ||--o{ conversations : owns
    conversations ||--o{ messages : contains
    conversations ||--o| session_contexts : has
    conversations ||--o{ workflow_executions : produces
    conversations ||--o{ agent_runs : records
    workflow_executions |o--o{ agent_runs : "approval of"

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
        string provider "google"
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
        uuid conversation_id FK
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
    agent_runs {
        uuid id PK
        uuid user_id FK
        uuid conversation_id FK
        uuid workflow_id FK
        string request_id
        string status
        string intent
        string agent
        string approval_status
        string action_status
        json steps
        json tools
        json llm_calls
        int duration_ms
    }
```

| Migration | Contents |
|---|---|
| `alembic/versions/0001_initial.py` | Users, credentials, conversations, messages, session contexts, workflow executions |
| `alembic/versions/0002_agent_runs.py` | Execution history (`agent_runs`) |

Every user-owned query is filtered by `user_id`. A resource owned by another user returns 404, the same as one that doesn't exist. `GET /api/health/ready` reports whether the database is at the expected revision (`0002`).

### Security model

| Concern | Mitigation |
|---|---|
| OAuth CSRF | The signed `state` token must match an HTTP-only, `SameSite=Lax` nonce cookie (`oauth_nonce`, 10-minute lifetime, scoped to `/api/auth`, `Secure` in production), compared in constant time. |
| Token leakage | Google access and refresh tokens are **Fernet-encrypted at rest** and never sent to the frontend, through MCP arguments, into logs or into execution history. |
| Session token | After the callback, the app's own JWT is delivered in the **URL fragment** (`/auth/callback#token=...`), which browsers never send to servers or write to access logs. |
| Least privilege | The requested scopes are `gmail.readonly`, `gmail.send`, `gmail.compose` and `calendar`. Legacy `gmail.modify` grants are still accepted. |
| Secrets in config | `JWT_SECRET` must be at least 32 characters, and `TOKEN_ENCRYPTION_KEY` must be a valid Fernet key. The app refuses to start otherwise. |
| Tenant isolation | Every conversation, workflow, execution and credential lookup is filtered by the authenticated `user_id`. |
| Unsafe writes | All Gmail and Calendar writes go through the approval workflow. The read-only REST routes can't write. |
| Untrusted LLM output | Pydantic validation, email normalisation (a name in an email field is dropped), deterministic missing-parameter checks, and answers grounded in the email text. |
| Prompt injection via email | Email and event text is labelled as untrusted data in prompts and MCP server instructions. It can only produce *proposals*, which you still approve. |
| Error leakage | Exception handlers (`api/errors.py`) return only `{code, message}`, with no stack traces or provider details. |
| Container | The backend image runs as a non-root `app` user. |

---

## Frontend

The single-page React app lives in `frontend/src/`.

| File | Contents |
|---|---|
| `main.jsx` | Mounts `<App />` |
| `App.jsx` | Login and OAuth callback, the workspace (`Dashboard`), chat bubbles, approval buttons, live step list, Activity view |
| `api.js` | `request()` with timeouts and typed `ApiError`s, `streamTurn()` (NDJSON reader with stall watchdog), `waitForExecution()` (recovery polling) |
| `styles.css` | All styling |
| `App.test.jsx` | Vitest and Testing Library suite |

Inside the workspace:

- The **Assistant** tab holds the conversation thread, a composer (Enter sends, Shift+Enter adds a new line, 2,000-character limit), a conversation list, live progress steps while a turn runs, Approve and Reject buttons on proposals, one-click replies for calendar offers, and Retry on messages that weren't sent.
- The **Activity** tab shows execution history with per-run steps, tool calls and model calls.
- **Gmail** and **Calendar** tabs currently point you back to the Assistant.
- Connection pills in the header show whether Gmail and Calendar are connected.

The session JWT is kept in `localStorage` under the key `orbit_token`. A 401 response signs you out, with a "session expired" message when the token has expired.

---

## Project structure

```text
agentic-workflow-assistant/
├── docker-compose.yml               # db (Postgres 16), backend, frontend
├── vercel.json                      # Vercel Services: "web" (Vite) + "api" (FastAPI), routing
├── .vercelignore                    # keeps .env files, venvs and caches out of uploads
├── README.md
├── backend/
│   ├── Dockerfile                   # python:3.12-slim, non-root, runs migrations then uvicorn
│   ├── .python-version              # pins Python 3.12 (Vercel)
│   ├── requirements.txt
│   ├── .env.example                 # copy to .env (never commit .env)
│   ├── alembic.ini
│   ├── pytest.ini
│   ├── alembic/
│   │   ├── env.py
│   │   └── versions/
│   │       ├── 0001_initial.py      # core schema
│   │       └── 0002_agent_runs.py   # execution history
│   └── app/
│       ├── main.py                  # app factory: logging, CORS, request-ID middleware, error handlers
│       ├── api/
│       │   ├── router.py            # mounts all routers under /api
│       │   ├── deps.py              # current user, Google services, MCP gateway factory
│       │   ├── errors.py            # exception -> {code, message} responses
│       │   └── routes/
│       │       ├── auth.py          # Google OAuth login/callback/config, /me, timezone, disconnect
│       │       ├── conversations.py # conversation CRUD
│       │       ├── chat.py          # one turn, plain or streamed (NDJSON)
│       │       ├── workflows.py     # approval proposals and decisions
│       │       ├── executions.py    # execution history (Activity)
│       │       ├── gmail.py         # read-only Gmail endpoints
│       │       ├── calendar.py      # read-only Calendar endpoints
│       │       ├── intent.py        # intent analysis and LLM routing info
│       │       ├── mcp.py           # live MCP tool discovery
│       │       └── health.py        # liveness and readiness self-check
│       ├── agents/
│       │   ├── base.py              # AgentContext, AgentOutcome, ToolCall, Proposal contracts
│       │   ├── service_selector.py  # Gmail vs Calendar routing
│       │   ├── intent_agent.py      # intent and entity recognition
│       │   ├── context_agent.py     # memory, pronouns, candidates, pending requests
│       │   ├── dates.py             # relative date and time resolution
│       │   ├── gmail_agents.py      # per-intent Gmail agents, email -> calendar offer
│       │   ├── calendar_agents.py   # per-intent Calendar agents
│       │   ├── email_understanding_agent.py  # grounded email analysis and Q&A
│       │   ├── summarization_agent.py
│       │   └── task_agent.py        # email composition
│       ├── workflows/
│       │   ├── graph.py             # LangGraph nodes, routing, compilation
│       │   ├── state.py             # WorkflowState and WorkflowDeps
│       │   └── chat_service.py      # run_turn(), record_decision()
│       ├── mcp/
│       │   ├── gmail_server.py      # Gmail MCP server (6 tools)
│       │   ├── calendar_server.py   # Calendar MCP server (6 tools)
│       │   ├── client.py            # MCPGateway, stdio and in-process factories
│       │   ├── runtime.py           # per-user server scopes
│       │   ├── executors.py         # approved action -> MCP tool call
│       │   └── errors.py            # coded tool errors
│       ├── integrations/
│       │   ├── http.py              # shared Google HTTP client and error mapping
│       │   ├── errors.py            # GoogleAuthError, GoogleNotFoundError, ...
│       │   ├── gmail/               # client.py (REST), service.py, mime.py (MIME build/parse)
│       │   └── calendar/            # client.py, service.py, matching.py, timeutil.py, models.py
│       ├── services/
│       │   ├── auth_service.py      # user upsert, credential storage, connection status
│       │   ├── google_oauth.py      # auth URL, code exchange, ID-token verification, revoke
│       │   ├── token_service.py     # decrypt and refresh access tokens
│       │   ├── workflow_service.py  # propose, reject, execute
│       │   ├── execution_history.py # agent_runs bookkeeping
│       │   └── llm/
│       │       ├── router.py        # role -> provider/model, metrics
│       │       ├── base.py          # provider interface, retries, time budget, errors
│       │       ├── openrouter.py
│       │       ├── gemini.py
│       │       ├── prompts.py       # system and user prompts
│       │       ├── structured.py    # validated structured output
│       │       └── jsonutil.py      # robust JSON extraction
│       ├── core/
│       │   ├── config.py            # Pydantic Settings, validated at startup
│       │   ├── security.py          # JWT, OAuth state, Fernet helpers
│       │   ├── validation.py        # email and time normalisation
│       │   ├── log_config.py        # JSON/text logging, redaction, request middleware
│       │   └── logging_context.py   # request-scoped log context (contextvars)
│       ├── database/
│       │   ├── base.py              # declarative base, portable JSON type
│       │   ├── models.py            # ORM models
│       │   └── session.py           # engine and get_db dependency
│       ├── schemas/                 # Pydantic request/response models
│       │   ├── chat.py
│       │   ├── conversation.py
│       │   ├── execution.py
│       │   ├── intent.py            # intents, entities, required parameters, questions
│       │   ├── user.py
│       │   └── workflow.py
│       └── tests/                   # pytest suite: 29 modules, SQLite + fakes
└── frontend/
    ├── Dockerfile                   # node:22-alpine, Vite dev server
    ├── .env.production              # VITE_API_URL=/api
    ├── package.json
    ├── index.html
    └── src/
        ├── main.jsx                 # entry point
        ├── App.jsx                  # login, callback, workspace, approvals, Activity
        ├── api.js                   # HTTP client, NDJSON streaming, recovery
        ├── App.test.jsx             # Vitest suite
        └── styles.css
```

---

## Local development

### Prerequisites

- Python 3.12
- Node.js 20 or newer, with npm
- PostgreSQL 16, or the Compose `db` service
- A Google Cloud project with OAuth credentials (for live Google features)
- An OpenRouter or Gemini API key (for live LLM calls)

### 1. Backend environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
Copy-Item backend\.env.example backend\.env   # then fill in the secrets
```

On macOS or Linux, use `source .venv/bin/activate` and `cp`.

### 2. Database

```powershell
docker compose up -d db          # or point DATABASE_URL at your own PostgreSQL
cd backend
python -m alembic upgrade head
```

The default URL is `postgresql+psycopg://assistant:assistant@localhost:5432/assistant`. Plain `postgres://` URLs are rewritten to the psycopg driver form automatically.

### Google OAuth configuration

1. In Google Cloud Console, create or select a project.
2. Enable the **Gmail API** and the **Google Calendar API**.
3. Configure the OAuth consent screen. While the app is in testing mode, add your account as a test user.
4. Create an **OAuth client ID** of type *Web application*.
5. Add the authorized JavaScript origin `http://localhost:5173` and the redirect URI `http://localhost:8000/api/auth/google/callback`.
6. Put the client ID and secret into `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

The app requests these scopes: `openid`, `userinfo.email`, `userinfo.profile`, `gmail.readonly`, `gmail.send`, `gmail.compose` (drafts) and `calendar` (events and free/busy). `GET /api/auth/google/config` returns the exact client ID, redirect URI, JavaScript origin and scopes the running backend expects, along with any configuration problems.

### 3. Run the backend

```powershell
cd backend
python -m uvicorn app.main:app --reload
```

Check http://localhost:8000/api/health (liveness) and http://localhost:8000/api/health/ready (database, migrations, OAuth, LLM and MCP configuration). The backend terminal logs workflow transitions, model calls, MCP calls, approvals and Google API activity with timestamps and request IDs. Message contents and credentials are not logged.

### 4. Run the frontend

```powershell
cd frontend
npm install
npm run dev
```

`npm run dev` serves the app at http://localhost:5173 and opens it in your default browser. In development the frontend calls `http://localhost:8000/api` unless `VITE_API_URL` is set.

---

## Deploying to Vercel

The repository deploys as a single Vercel project using [Vercel Services](https://vercel.com/docs/services), configured in `vercel.json`:

| Service | Root | Serves |
|---|---|---|
| `api` | `backend/` | FastAPI (`app.main:app`) as one Python function with a 300-second `maxDuration`, serving `/api/*`, `/docs` and `/openapi.json` |
| `web` | `frontend/` | The Vite static build for every other path, with an SPA fallback to `index.html` |

Because both services share a domain, the frontend calls the relative path `/api` (set in `frontend/.env.production`), so no cross-origin setup is needed. Tests, Alembic files and the Dockerfile are excluded from the function bundle.

**Database.** Neon Postgres from the Vercel Marketplace injects `DATABASE_URL`. Migrations do not run on deploy. Run them from your machine whenever the schema changes:

```powershell
npx vercel env pull .env.local                        # includes DATABASE_URL_UNPOOLED
$env:DATABASE_URL = (Select-String '^DATABASE_URL_UNPOOLED=' .env.local).Line.Split('=',2)[1].Trim('"')
cd backend; python -m alembic upgrade head
```

**Production environment variables.** Set these under Project, Settings, Environment Variables:

- `APP_ENV=production` turns on secure cookies, JSON logs and the production OAuth checks.
- `MCP_TRANSPORT=inprocess` runs the MCP servers inside the function instead of as subprocesses.
- `FRONTEND_URL`, `CORS_ORIGINS` and `GOOGLE_REDIRECT_URI` point at the production domain. If any of them is left at a localhost default, it is replaced with `https://$VERCEL_PROJECT_PRODUCTION_URL`, and the problem is logged at CRITICAL.
- `JWT_SECRET` and `TOKEN_ENCRYPTION_KEY` should be different from your local values.
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and the LLM keys and models.

**Google OAuth.** Edit the OAuth client in Google Cloud Console (APIs & Services, Credentials):

- Authorized JavaScript origin: `https://agentic-workflow-assistant.vercel.app`
- Authorized redirect URI: `https://agentic-workflow-assistant.vercel.app/api/auth/google/callback`

The callback path is fixed by the route (`OAUTH_CALLBACK_PATH` in `core/config.py`). After deploying, open `/api/health/ready`: it should report `"problems": []` under `oauth`, and `migrations_current: true`.

**Logs.** Filter with `npx vercel logs --query 'level:error'`, or search by `request_id`, which is also returned in the `X-Request-ID` header. `LOG_LEVEL` and `LOG_FORMAT` override the defaults.

**Redeploy.** Push to `main` (the GitHub repository is connected), or run `npx vercel deploy --prod`.

---

## Configuration reference

All backend settings are read from `backend/.env` (or the process environment) by `app/core/config.py` and validated at startup.

| Variable | Default | Required | Description |
|---|---|---|---|
| `APP_ENV` | `development` | No | `production` enables secure cookies, JSON logs and production OAuth checks. |
| `DATABASE_URL` | local `assistant` DB | Yes | SQLAlchemy URL. `postgres://` is rewritten to `postgresql+psycopg://`. |
| `JWT_SECRET` | none | **Yes** | At least 32 characters. Signs app JWTs and OAuth state. |
| `JWT_ALGORITHM` | `HS256` | No | JWT algorithm. |
| `JWT_EXPIRE_MINUTES` | `480` | No | Session lifetime. |
| `TOKEN_ENCRYPTION_KEY` | none | **Yes** | Fernet key used to encrypt Google tokens. |
| `GOOGLE_CLIENT_ID` | none | For OAuth | OAuth web client ID. |
| `GOOGLE_CLIENT_SECRET` | none | For OAuth | OAuth web client secret. |
| `GOOGLE_REDIRECT_URI` | `http://localhost:8000/api/auth/google/callback` | No | Must match the URI registered in Google Cloud; the path must be `/api/auth/google/callback`. |
| `FRONTEND_URL` | `http://localhost:5173` | No | Where the OAuth callback redirects to. |
| `CORS_ORIGINS` | `http://localhost:5173` | No | Comma-separated list of allowed origins. |
| `VERCEL_PROJECT_PRODUCTION_URL` | set by Vercel | No | Used in production as a fallback for URLs left at localhost defaults. |
| `OPENROUTER_API_KEY` | none | For OpenRouter | OpenRouter key. |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | No | |
| `OPENROUTER_JSON_MODE` | `false` | No | Sends `response_format`. Leave it off for DeepSeek R1. |
| `GEMINI_API_KEY` | none | For Gemini | Gemini key. |
| `GEMINI_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta` | No | |
| `LLM_PROVIDER` | `openrouter` | No | `openrouter` or `gemini`. |
| `LLM_MODEL` | `deepseek/deepseek-r1-0528` | No | Default model for every role. |
| `INTENT_LLM_PROVIDER` / `INTENT_LLM_MODEL` | blank | No | Override for the intent role. |
| `SUMMARY_LLM_PROVIDER` / `SUMMARY_LLM_MODEL` | blank | No | Override for the summarization and email-understanding roles. |
| `TASK_LLM_PROVIDER` / `TASK_LLM_MODEL` | blank | No | Override for the email-composition role. |
| `LLM_TIMEOUT_SECONDS` | `120` | No | Timeout for each provider HTTP attempt. |
| `LLM_MAX_RETRIES` | `2` | No | Retries after the first attempt. |
| `LLM_TURN_BUDGET_SECONDS` | `240` | No | Total LLM time allowed per conversation turn. |
| `LLM_MAX_TOKENS` | `4096` | No | Includes reasoning tokens for R1-style models. |
| `MCP_TRANSPORT` | `stdio` | No | `stdio` or `inprocess`. |
| `MCP_TIMEOUT_SECONDS` | `60` | No | Timeout for each tool call. |
| `LOG_LEVEL` | `INFO` | No | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |
| `LOG_FORMAT` | `json` in production, otherwise `text` | No | Log line format. |

Frontend: `VITE_API_URL` defaults to `http://localhost:8000/api` in development and to `/api` in production builds.

**Example: Gemini for intent recognition, DeepSeek for everything else**

```env
LLM_PROVIDER=openrouter
LLM_MODEL=deepseek/deepseek-r1-0528
INTENT_LLM_PROVIDER=gemini
INTENT_LLM_MODEL=gemini-2.0-flash
```

---

## API reference

All routes are prefixed with `/api`. Everything except the health and OAuth endpoints requires `Authorization: Bearer <jwt>`. Full request and response schemas are at `/docs` (Swagger) and `/redoc`. You can send an `X-Request-ID` header of 8 to 64 characters from `[A-Za-z0-9._-]` to correlate a request; the response always returns one.

Errors share one shape:

```json
{ "detail": { "code": "google_reconnect_required", "message": "Reconnect the required Google service." } }
```

### Authentication

| Method | Path | Description |
|---|---|---|
| `GET` | `/auth/google/login` | Sets the CSRF nonce cookie and redirects to Google consent. |
| `GET` | `/auth/google/callback` | Exchanges the code, stores encrypted tokens, and redirects to `FRONTEND_URL/auth/callback#token=...`. |
| `GET` | `/auth/google/config` | Public OAuth settings in effect (client ID, redirect URI, origin, scopes, problems). Never returns the secret. |
| `GET` | `/auth/me` | Profile plus Gmail and Calendar connection status. |
| `PATCH` | `/auth/me/timezone` | Sets the IANA timezone, for example `{"timezone": "Asia/Kolkata"}`. |
| `POST` | `/auth/logout` | Client-side logout (JWTs are stateless). |
| `DELETE` | `/auth/google/connection` | Revokes the Google grant and deletes the stored credentials. |

### Conversations and chat

| Method | Path | Description |
|---|---|---|
| `POST` | `/conversations` | Create a conversation. |
| `GET` | `/conversations` | List your conversations, most recent first. |
| `GET` | `/conversations/{id}` | One conversation with its messages. |
| `DELETE` | `/conversations/{id}` | Delete a conversation and everything in it. |
| `POST` | `/conversations/{id}/messages` | Run one turn through LangGraph. Body: `{"content": "..."}` (1 to 2,000 characters). |
| `POST` | `/conversations/{id}/messages/stream` | The same turn, streamed as NDJSON (see [Streaming and recovery](#streaming-and-recovery)). Used by the web UI. |

Example turn response:

```json
{
  "status": "awaiting_approval",
  "user_message": { "id": "...", "role": "user", "content": "Email priya@example.com that the demo moved to Friday" },
  "assistant_message": { "id": "...", "role": "assistant", "content": "Send this email to priya@example.com?\nSubject: ...\n\nApprove to go ahead, or reject to cancel. Nothing happens until you approve.", "meta": { "trace": [ ... ] } },
  "workflow": { "id": "...", "status": "awaiting_approval", "service": "gmail", "intent": "send_email" },
  "execution_id": "..."
}
```

### Workflows (approvals)

| Method | Path | Description |
|---|---|---|
| `POST` | `/workflows` | Create a proposal directly. |
| `GET` | `/workflows` | List your workflows, newest first. |
| `GET` | `/workflows/{id}` | Get one workflow. |
| `POST` | `/workflows/{id}/decision` | `{"approve": true}` runs the action through MCP; `{"approve": false}` rejects it. Returns 409 if the workflow is no longer pending. |
| `DELETE` | `/workflows/{id}` | Reject a pending workflow. |

### Execution history

| Method | Path | Description |
|---|---|---|
| `GET` | `/executions?limit=50&offset=0&request_id=...` | Your runs, newest first. `limit` can be 1 to 200. `request_id` finds the run started by a specific request. |
| `GET` | `/executions/{id}` | One run with its steps, tool calls and model calls. Another user's run returns 404. |

### Read-only Gmail and Calendar

| Method | Path | Description |
|---|---|---|
| `GET` | `/gmail/messages?q=...&max_results=10` | Search Gmail (1 to 25 results). |
| `GET` | `/gmail/messages/{id}` | Read a message. |
| `GET` | `/gmail/threads/{id}` | Read a thread. |
| `GET` | `/calendar/events?days=7&q=...` | List upcoming events (1 to 90 days ahead). |
| `GET` | `/calendar/next` | The next event. |
| `GET` | `/calendar/availability?start=...&end=...` | Free/busy and free slots. Naive datetimes use your timezone. |

### Diagnostics

| Method | Path | Description |
|---|---|---|
| `POST` | `/intent/analyze` | Classify a message without taking any action. |
| `GET` | `/llm/config` | Effective provider and model per role, and whether each provider has a key. |
| `GET` | `/mcp/tools` | Tools discovered live from both MCP servers. |
| `GET` | `/health` | Liveness. |
| `GET` | `/health/ready` | Readiness: database connectivity and migration revision, OAuth URLs and problems, whether secrets are set (never their values), LLM provider, MCP transport. Returns 503 if the database is unreachable. |

---

## Example conversations

**Clarification, approval and follow-up**

```text
You:        Schedule a meeting with Arjun tomorrow
Assistant:  What time?                                   ← missing parameter; request kept pending
You:        4pm, invite arjun@example.com
Assistant:  Create "Meeting with Arjun", Sun 29 Nov, 16:00–17:00, attendee arjun@example.com.
            Approve to go ahead, or reject to cancel.    ← awaiting_approval
[Approve]
Assistant:  Created "Meeting with Arjun" starting 2026-11-29T16:00:00+05:30.
You:        Actually move it to 5                        ← "it" resolved from memory (last_event_id)
Assistant:  Move "Meeting with Arjun" to Sun 29 Nov, 17:00? Approve or reject.
```

**Grounded email question that leads to a calendar event**

```text
You:        Did Priya email me about the design review?
Assistant:  Yes. Priya asked to hold the design review on Monday at 11:00.
            From: Priya <priya@example.com>
            Subject: Design review
            Email content: ...
            Would you like me to add this meeting to your Google Calendar?
            [Yes, add it to my calendar] [No thanks]
You:        Yes, add it to my calendar                    ← continues a pending create_event
Assistant:  Create "Design review", Mon 30 Nov, 11:00–12:00 ... Approve or reject.
```

---

## Testing

The backend suite has **323 tests in 29 modules**. It runs against isolated SQLite databases with fake Google and LLM providers, so it needs no network access or credentials.

```powershell
cd backend
python -m pytest -q                      # full suite
python -m pytest app/tests/test_mcp.py   # a single module
python -m pytest --cov=app               # with coverage
```

| Area | Test modules |
|---|---|
| LangGraph and chat | `test_chat_workflow.py`, `test_workflows.py`, `test_conversations.py`, `test_streaming.py` |
| Agents | `test_intent.py`, `test_context_agent.py`, `test_service_selector.py`, `test_specialized_agents.py`, `test_email_understanding_agent.py` |
| Email grounding | `test_email_search_grounding.py` (full-body retrieval, broader-query fallback, answers grounded in the email text, calendar offers) |
| MCP | `test_mcp.py` (protocol, discovery, unknown-tool rejection, approval execution) |
| LLM | `test_llm.py`, `test_model_roles.py`, `test_jsonutil.py` |
| Google integrations | `test_gmail_client.py`, `test_gmail_service.py`, `test_calendar_service.py`, `test_google_http.py`, `test_google_routes.py`, `test_mime.py`, `test_matching.py`, `test_timeutil.py` |
| Auth and security | `test_auth_flow.py`, `test_oauth_production.py`, `test_security.py`, `test_token_service.py`, `test_validation.py` |
| Observability | `test_logging.py` (redaction, JSON format, correlation across threads), `test_executions.py` |

Shared fixtures are in `conftest.py`, and the fake providers are in `fakes.py`.

The frontend has 24 tests using Vitest, jsdom and Testing Library with a mocked `fetch`. They cover chat submission, live progress, follow-ups, approvals, stream recovery, errors and the Activity tab.

```powershell
cd frontend
npm test          # vitest run
npm run build     # production build check
```

---

## Extending the assistant

### Add a new intent (for example `archive_email`)

1. **Schema.** Add the intent to `GMAIL_INTENTS` and `Intent` in `schemas/intent.py`. List its required parameters in `REQUIREMENTS` and their questions in `QUESTIONS`.
2. **Prompt.** Describe the intent and its entities in `services/llm/prompts.py` so the Intent Recognition Agent can produce it. If the Service Selector should spot it, add keywords to `service_selector.py`.
3. **MCP tool.** Add a `@server.tool(...)` to `mcp/gmail_server.py` with typed, bounded arguments and the right read-only, write or destructive annotation. Implement it in `integrations/gmail/service.py`. If the action needs a new OAuth scope, add it to `GOOGLE_SCOPES`.
4. **Agent.** Subclass `OperationAgent` in `agents/gmail_agents.py`. Implement `step()` so it returns `ToolCall`s, then a `result` or a `Proposal`, and register the agent in `build_gmail_agents()`.
5. **Approval (writes only).** Handle the intent in `services/workflow_service.py` (`execute()`), add a method to `MCPGmailExecutor`, and add a result message in `chat_service._result_text()`.
6. **UI labels (optional).** Add a tool label in `TOOL_LABELS` (`workflows/graph.py`) so the live progress view reads well.
7. **Tests.** Cover the schema requirements, agent steps and MCP tool using the existing fakes.

### Add an LLM provider

Implement the `LLMProvider` protocol from `services/llm/base.py`, using `post_json()` to get retries and the time budget. Register it in `LLMRouter._provider()`, and allow its name in the provider validator in `core/config.py`.

### Schema changes

```powershell
cd backend
python -m alembic revision --autogenerate -m "describe change"
python -m alembic upgrade head
```

Then update `EXPECTED_DB_REVISION` in `api/routes/health.py`. A test checks that it matches the newest migration.

---

## Troubleshooting and error codes

| Code or symptom | Meaning | Fix |
|---|---|---|
| App fails on startup with a validation error | `JWT_SECRET` is too short or `TOKEN_ENCRYPTION_KEY` is not a valid Fernet key | Regenerate both with the commands in [Quick start](#quick-start-with-docker). |
| `/login?error=not_configured` | Google client ID, secret or redirect URI is missing | Set them in `backend/.env`. |
| `/login?error=invalid_state` | The OAuth nonce cookie is missing, expired or doesn't match | Start again from the login page in the same browser, and don't mix `localhost` with `127.0.0.1`. |
| `/login?error=oauth_failed` | Code exchange, ID-token verification or saving the credentials failed | Check the backend logs; the Google error code (for example `invalid_grant`) is logged. |
| `/login?error=access_denied` | Consent was declined in Google | Sign in again and grant the requested scopes. |
| `redirect_uri_mismatch` (from Google) | The redirect URI isn't registered | Add the exact value from `/api/auth/google/config` to the OAuth client. |
| `session_expired` (401) | The app JWT has expired | Sign in again. The lifetime is set by `JWT_EXPIRE_MINUTES`. |
| `google_reconnect_required` | Token revoked or expired, or a scope is missing | Sign in with Google again. |
| `google_api_not_enabled` / `service_disabled` | Gmail or Calendar API is disabled in the GCP project | Enable it in Google Cloud Console. |
| `google_rate_limited` | Google quota was hit | Wait and retry. |
| `verification_failed` | Google accepted a calendar write but re-reading it didn't confirm it | Check the calendar; the assistant didn't report it as done. |
| `llm_not_configured` | No API key for the configured provider, or the provider rejected it | Set `OPENROUTER_API_KEY` or `GEMINI_API_KEY`, then check `/api/llm/config`. |
| `llm_unavailable` / `llm_invalid_response` | Provider error, exhausted time budget or unusable output | Retry, raise `LLM_TIMEOUT_SECONDS` or `LLM_MAX_TOKENS`, or route the role to another model. |
| `mcp_connection_failed` | An MCP server couldn't start or respond | Check backend logs; set `MCP_TRANSPORT=inprocess` to isolate the problem. |
| `timed_out` in Activity | The turn exceeded the serverless time limit | Retry; consider a faster model for the `intent` or `summarization` roles. |
| "Connection dropped… result was loaded from the server" | The stream was lost but the turn finished | Nothing to do; the outcome shown is the real one. |
| `/api/health/ready` returns 503 | The database is unreachable | Check `DATABASE_URL` and that PostgreSQL is running. |
| `migrations_current: false` | The database schema is behind the code | Run `python -m alembic upgrade head`. |
| Workflow decision returns 409 | The workflow is no longer `awaiting_approval` | It was already approved or rejected; reload the conversation. |

Every error response and log line carries a `request_id`. Search the backend logs for it to see the full trace of a failed request.

---

## Known limitations

- **Stateless logout.** JWTs are not revoked on the server. Logging out discards the token in the browser, and tokens expire after `JWT_EXPIRE_MINUTES`.
- **Token storage.** The frontend keeps the JWT in `localStorage`. An HTTP-only cookie session would be harder to steal through XSS.
- **Primary calendar only.** Only the user's primary calendar is used. All-day events can't be rescheduled by the Calendar agent.
- **Bounded reads.** Calendar listing covers 1 to 90 days ahead. Email questions read the top five matches out of up to ten.
- **Placeholder tabs.** The Gmail and Calendar tabs in the UI don't browse data yet; everything goes through the Assistant.
- **Unpinned frontend dependencies.** Runtime packages in `frontend/package.json` use `latest`. Pin them for reproducible builds.
- **Development server in Docker.** The frontend container runs the Vite dev server. For production, serve the `npm run build` output (as Vercel does).
- **Mocked tests.** The test suites use fakes, so they don't prove the live Google or LLM integrations work. Those need real credentials and a consenting account.
- **Approved writes are real.** Approved actions really send email and change calendars. Use a test Google account while developing.
