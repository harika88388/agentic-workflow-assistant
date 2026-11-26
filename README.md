# Agentic Workflow Assistant

A FastAPI application that turns natural-language requests into safe, context-aware Gmail and Google Calendar workflows. Users sign in with Google, interact through a React workspace, and review sensitive email or calendar actions before the backend executes them.

The project uses PostgreSQL for users, conversations, structured context, encrypted Google credentials, and workflow records. LangGraph orchestrates specialized agents; the official Python MCP SDK exposes Gmail and Calendar tools; and the existing LLM router selects OpenRouter/DeepSeek or Gemini models by responsibility.

## Key features

### 1. Backend engineering

- FastAPI REST API with JWT authentication and request-scoped database sessions.
- SQLAlchemy 2.0 models and PostgreSQL 16 support in Docker; PostgreSQL 18 was also connection-tested locally.
- Alembic migrations, including the initial `0001` schema migration.
- Pydantic validation, structured API errors, user ownership checks and readiness checks.

### 2. LangGraph agentic orchestration

The chat workflow uses LangGraph and includes:

- Service Selector Agent
- Intent Recognition Agent
- Context Enhancement Agent
- Specialized Gmail agents
- Specialized Google Calendar agents

### 3. MCP integration

- Official Python MCP SDK.
- Separate Gmail and Google Calendar MCP servers.
- MCP client-side tool discovery and invocation.
- Configurable `stdio` subprocess transport or `inprocess` test transport.
- Credentials are loaded in user-specific server scopes and are not sent as MCP tool arguments.

The Gmail server registers 6 tools: `search_emails`, `read_email`, `get_email_thread`, `send_email`, `reply_email`, and `create_draft`.

The Calendar server registers 6 tools: `list_events`, `get_event`, `create_event`, `update_event`, `delete_event`, and `check_availability`.

### 4. Multi-model orchestration

The existing `LLMRouter` supports:

- OpenRouter / DeepSeek.
- Gemini.
- Intent recognition.
- Summarization.
- Task automation.

Provider and model selection is configurable globally or independently for each responsibility.

### 5. Google integrations

- Google OAuth 2.0 Authorization Code flow.
- Gmail search, read, thread, send, reply and draft operations.
- Calendar listing, retrieval, creation, rescheduling, cancellation and availability operations.
- Fernet-encrypted access and refresh tokens at rest.
- User-specific Google credential lookup and refresh handling.

### 6. Context-aware automation

Conversations, messages and session context are persisted in PostgreSQL. The workflow can continue pending requests, resolve references such as "him", "it", and "that meeting", retain candidate selections, detect missing parameters and ask clarifying questions. It does not use RAG or a vector database.

## System architecture

```mermaid
flowchart TD
    U[React user workspace] -->|POST /api/conversations/:id/messages| API[FastAPI]
    API --> C[Load conversation context]
    C --> S[Service Selector Agent]
    S --> I[Intent Recognition Agent]
    I --> X[Context Enhancement Agent]
    X --> V[Missing parameter detection]
    V -->|clarification| R[Response generation]
    V -->|Gmail operation| G[Specialized Gmail agent]
    V -->|Calendar operation| CA[Specialized Calendar agent]
    G -->|read operation| MCP[MCP client]
    CA -->|read operation| MCP
    G -->|sensitive write proposal| A[Approval workflow]
    CA -->|sensitive write proposal| A
    MCP --> GS[Gmail MCP server]
    MCP --> CS[Calendar MCP server]
    GS --> GA[Gmail API]
    CS --> CV[Google Calendar API]
    GA --> GS
    CV --> CS
    GS --> MCP
    CS --> MCP
    MCP --> G
    MCP --> CA
    G --> R
    CA --> R
    R --> P[PostgreSQL persistence]
    A -->|approved| MCP
    A -->|rejected| R
```

The conceptual route is implemented by `app/workflows/graph.py` with LangGraph nodes. Read-only Gmail and Calendar operations can execute during a chat turn. Send, reply, draft, create, reschedule and cancel operations create an `awaiting_approval` workflow and execute only after an explicit decision.

## Agent architecture

- **Service Selector Agent**: uses deterministic service signals and pending-conversation context to choose Gmail or Calendar, then reconciles that choice with the recognized intent.
- **Intent Recognition Agent**: uses the LLM router and validated JSON output to identify operations and entities. Missing parameters are enforced deterministically rather than trusted from model output.
- **Context Enhancement Agent**: merges the latest conversation history, session memory and pending entities. It resolves pronouns, candidate selections and relative dates within the user's timezone.
- **Gmail agents**: search, read, reply and summarize emails, and propose sending, replying or creating drafts.
- **Calendar agents**: list events, check availability, create events, locate events safely and propose rescheduling or cancellation.
- **LangGraph orchestrator**: controls context loading, routing, agent iterations, MCP calls, approval proposals, response generation and PostgreSQL persistence.

## MCP architecture

MCP servers are implemented with the official Python MCP SDK in `backend/app/mcp/`.

- `gmail_server.py` exposes Gmail operations.
- `calendar_server.py` exposes Calendar operations.
- `client.py` provides discovery and invocation through `MCPGateway`.
- `executors.py` adapts approved workflow actions to MCP tool calls.
- `runtime.py` binds stdio servers to a specific user through `MCP_USER_ID`.

The default `MCP_TRANSPORT=stdio` starts a server subprocess for each Gmail or Calendar connection. `MCP_TRANSPORT=inprocess` is available for tests and single-process deployments. MCP sessions discover tools with `tools/list` and reject calls to tools the server did not advertise.

## Multi-model architecture

`backend/app/services/llm/router.py` defines three model roles:

| Role | Default responsibility | Environment override |
|---|---|---|
| Intent | Intent and entity recognition | `INTENT_LLM_PROVIDER`, `INTENT_LLM_MODEL` |
| Summarization | Gmail summarization | `SUMMARY_LLM_PROVIDER`, `SUMMARY_LLM_MODEL` |
| Task automation | Email composition | `TASK_LLM_PROVIDER`, `TASK_LLM_MODEL` |

The global provider is `LLM_PROVIDER` and the global model is `LLM_MODEL`. Provider/model overrides may be set independently when the override differs consistently from the global provider configuration.

## Gmail and Calendar integrations

Google OAuth requests the following scopes:

- `openid`
- `https://www.googleapis.com/auth/userinfo.email`
- `https://www.googleapis.com/auth/userinfo.profile`
- `https://www.googleapis.com/auth/gmail.modify`
- `https://www.googleapis.com/auth/calendar`

OAuth state is signed and matched against an HTTP-only nonce cookie. The application JWT is returned in the browser URL fragment after callback and is not sent to the server in that redirect request. Google access and refresh tokens are Fernet-encrypted in PostgreSQL and refreshed through the token service.

Gmail and Calendar REST clients use user-bound credentials. Write operations are not exposed as direct chat execution: they enter the approval workflow first.

## Conversation and context management

Conversation tables persist user-owned conversations and messages. `SessionContext` stores structured memory and pending request state for the conversation.

Context enhancement can:

- Continue a partially answered request.
- Resolve remembered recipient addresses and event candidates.
- Resolve relative dates in the user's IANA timezone.
- Request clarification for missing parameters or ambiguous event references.
- Store the final result and update memory after workflow approval decisions.

## Approval workflow

Approval is required for:

- Sending email.
- Replying to email.
- Creating drafts.
- Creating calendar events.
- Rescheduling calendar events.
- Cancelling calendar events.

A proposal is stored with status `awaiting_approval`. Approving it executes the operation through the corresponding MCP server. Rejecting it sets status `rejected` and executes no external write. Results, failures and rejection state are persisted in `WorkflowExecution` and appended to conversation messages. Idempotency keys prevent duplicate workflow proposals and Calendar creation uses workflow identity for idempotent event creation.

## Technology stack

- Python 3.12+ and FastAPI.
- PostgreSQL.
- SQLAlchemy 2.0 and Alembic.
- Pydantic 2 and Pydantic Settings.
- LangGraph.
- Official Python MCP SDK.
- Google OAuth, Gmail and Google Calendar APIs.
- OpenRouter with DeepSeek and Google Gemini providers.
- React, Vite and Lucide in the existing frontend.
- Docker Compose for local PostgreSQL, backend and frontend services.

## Installation and setup

### Prerequisites

- Python 3.12 or newer.
- PostgreSQL 16 or a compatible PostgreSQL installation.
- Node.js and npm for the React frontend.
- Docker Desktop only if using Docker Compose.
- Google OAuth credentials for live Google operations.
- An OpenRouter or Gemini API key for live model calls.

### Project structure

```text
backend/       FastAPI application, database, agents, MCP, integrations and tests
frontend/      React and Vite client
docker-compose.yml
```

### Windows PowerShell: backend environment

```powershell
cd c:\path\to\agentic-workflow-assistant
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
```

### PostgreSQL

Docker Compose includes PostgreSQL 16:

```powershell
cd c:\path\to\agentic-workflow-assistant
docker compose up -d db
```

Alternatively, configure a local PostgreSQL instance in `DATABASE_URL`. The default development URL is:

```text
postgresql+psycopg://assistant:assistant@localhost:5432/assistant
```

Only run migrations against a database you own and intend to use for development.

```powershell
cd backend
..\.venv\Scripts\python.exe -m alembic upgrade head
```

### Environment configuration

From the repository root:

```powershell
Copy-Item backend\.env.example backend\.env
```

Generate secrets without placing them in documentation:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Fill in `backend/.env` only with your own values. Never commit it.

### Google OAuth configuration

1. Create or select a Google Cloud project.
2. Enable Gmail API and Google Calendar API.
3. Configure an OAuth web client.
4. Add `http://localhost:8000/api/auth/google/callback` as an authorized redirect URI.
5. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `backend/.env`.

Start the backend and open `http://localhost:8000/api/auth/google/login` to begin consent.

### Run the backend

```powershell
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

API documentation: `http://localhost:8000/docs`

Health endpoints:

- `GET http://localhost:8000/api/health`
- `GET http://localhost:8000/api/health/ready`

### Run the frontend

```powershell
cd frontend
npm install
npm run dev
```

The frontend expects `VITE_API_URL=http://localhost:8000/api` by default.

### Docker Compose

```powershell
Copy-Item backend\.env.example backend\.env
# Set secrets and optional Google/LLM credentials in backend/.env
docker compose up --build
```

Docker Compose serves the frontend on `http://localhost:5173` and the backend on `http://localhost:8000`.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `APP_ENV` | No | Application environment; defaults to `development`. |
| `DATABASE_URL` | Yes for real PostgreSQL | SQLAlchemy PostgreSQL connection string. |
| `JWT_SECRET` | Yes | JWT signing secret; at least 32 characters. |
| `JWT_ALGORITHM` | No | JWT algorithm; defaults to `HS256`. |
| `JWT_EXPIRE_MINUTES` | No | Application JWT lifetime; defaults to `480`. |
| `TOKEN_ENCRYPTION_KEY` | Yes | Valid Fernet key for Google credential encryption. |
| `GOOGLE_CLIENT_ID` | Live OAuth | Google OAuth web client ID. |
| `GOOGLE_CLIENT_SECRET` | Live OAuth | Google OAuth web client secret. |
| `GOOGLE_REDIRECT_URI` | No | OAuth callback URL. |
| `FRONTEND_URL` | No | Frontend URL used after OAuth callbacks. |
| `CORS_ORIGINS` | No | Comma-separated allowed frontend origins. |
| `OPENROUTER_API_KEY` | Live OpenRouter | OpenRouter authentication. |
| `OPENROUTER_BASE_URL` | No | OpenRouter API base URL. |
| `OPENROUTER_JSON_MODE` | No | Enables JSON-mode preference where supported. |
| `GEMINI_API_KEY` | Live Gemini | Gemini authentication. |
| `GEMINI_BASE_URL` | No | Gemini API base URL. |
| `LLM_PROVIDER` | No | Default provider: `openrouter` or `gemini`. |
| `LLM_MODEL` | No | Default model name. |
| `INTENT_LLM_PROVIDER` / `INTENT_LLM_MODEL` | No | Intent-role provider and model override. |
| `SUMMARY_LLM_PROVIDER` / `SUMMARY_LLM_MODEL` | No | Summarization-role provider and model override. |
| `TASK_LLM_PROVIDER` / `TASK_LLM_MODEL` | No | Task-role provider and model override. |
| `LLM_TIMEOUT_SECONDS` | No | Provider HTTP timeout. |
| `LLM_MAX_RETRIES` | No | Provider retry count after the initial request. |
| `LLM_MAX_TOKENS` | No | Maximum provider response tokens. |
| `MCP_TRANSPORT` | No | `stdio` or `inprocess`; defaults to `stdio`. |
| `MCP_TIMEOUT_SECONDS` | No | MCP tool-call timeout. |

## API routes

All routes below are under `/api` unless shown otherwise.

### Authentication

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/auth/google/login` | Start Google OAuth. |
| `GET` | `/api/auth/google/callback` | Complete Google OAuth callback. |
| `GET` | `/api/auth/me` | Return profile and Gmail/Calendar connection status. |
| `POST` | `/api/auth/logout` | Discard the client-side JWT session. |
| `DELETE` | `/api/auth/google/connection` | Revoke and remove Google credentials. |
| `PATCH` | `/api/auth/me/timezone` | Update the user's IANA timezone. |

### Conversations and chat

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/conversations` | Create a conversation. |
| `GET` | `/api/conversations` | List owned conversations. |
| `GET` | `/api/conversations/{id}` | Retrieve an owned conversation and messages. |
| `DELETE` | `/api/conversations/{id}` | Delete an owned conversation. |
| `POST` | `/api/conversations/{id}/messages` | Run a conversation turn through LangGraph and MCP. |

### Workflows

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/workflows` | Create an approval workflow proposal. |
| `GET` | `/api/workflows` | List owned workflows. |
| `GET` | `/api/workflows/{id}` | Retrieve an owned workflow. |
| `POST` | `/api/workflows/{id}/decision` | Approve or reject with `{"approve": true|false}`. |
| `DELETE` | `/api/workflows/{id}` | Reject a pending workflow. |

### Read-only Gmail and Calendar

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/gmail/messages?q=...` | Search Gmail messages. |
| `GET` | `/api/gmail/messages/{id}` | Read a Gmail message. |
| `GET` | `/api/gmail/threads/{id}` | Read a Gmail thread. |
| `GET` | `/api/calendar/events?days=7&q=...` | List Calendar events. |
| `GET` | `/api/calendar/next` | Return the next event. |
| `GET` | `/api/calendar/availability?start=...&end=...` | Check Calendar availability. |

### Intent and MCP

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/intent/analyze` | Classify a message without performing an action. |
| `GET` | `/api/llm/config` | Show provider/model routing without secrets. |
| `GET` | `/api/mcp/tools` | Discover tools registered by the Gmail and Calendar MCP servers. |
| `GET` | `/api/health` | Liveness check. |
| `GET` | `/api/health/ready` | PostgreSQL readiness check. |

## Testing

Run the complete backend suite from the repository root:

```powershell
cd backend
..\.venv\Scripts\python.exe -m pytest -q --tb=line
```

Python syntax verification:

```powershell
..\.venv\Scripts\python.exe -m compileall -q app
```

The suite uses isolated SQLite fixtures and mocked Google/LLM behavior for external integrations. It tests LangGraph, context handling, MCP protocol behavior, approval gates, provider abstractions and ordinary backend functionality.

Build the frontend:

```powershell
cd frontend
npm run build
```

No frontend test script is currently defined in `package.json`.

## Known integration limitations

- PostgreSQL 18 connection and a harmless `SELECT 1` were verified locally. PostgreSQL is not treated as live-tested unless a real application database is used.
- Alembic reports migration `0001 (head)`; no migration was applied against an unknown existing database.
- Google OAuth, Gmail and Calendar live verification requires real Google OAuth credentials and an authorized account.
- OpenRouter/DeepSeek and Gemini live calls require API keys.
- Existing integration tests are mocked and must not be described as live external verification.
- Safe read operations can be tested when credentials are available; send, reply, draft, create, reschedule and cancel operations remain approval-gated and were not to be exercised externally.

## Known application limitations

- Logout is client-side because application sessions are stateless JWTs.
- Calendar listing is subject to configured look-ahead and result limits.
- All-day Calendar events cannot be rescheduled through the existing specialized agent.
- Live external integration accuracy depends on credentials and provider availability.
