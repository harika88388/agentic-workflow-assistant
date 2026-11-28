import React, { useEffect, useRef, useState } from "react";
import { Activity as ActivityIcon, AlertTriangle, CalendarDays, CheckCircle2, ChevronRight, Circle, Clock, Copy, HelpCircle, LogIn, Mail, MessageSquare, RefreshCw, Send, ShieldCheck, Wrench, XCircle } from "lucide-react";
import { API, apiLog, request, streamTurn, waitForExecution } from "./api.js";

/** One-line, user-safe description of an API failure, with the HTTP status and the request id to search logs for. */
export function describeError(err) {
  const extras = [err.status && `HTTP ${err.status}`, err.requestId && `request ID ${err.requestId}`].filter(Boolean);
  return extras.length ? `${err.message} (${extras.join(" · ")})` : err.message;
}

const LOGOUT_MESSAGES = {
  session_expired: "Your session has expired. Please sign in again.",
  unauthenticated: "You were signed out. Please sign in again.",
};

function AuthCallback({ onToken }) {
  useEffect(() => {
    const token = new URLSearchParams(window.location.hash.slice(1)).get("token");
    const error = new URLSearchParams(window.location.search).get("error");
    if (token) { window.history.replaceState({}, "", "/"); onToken(token); }
    else {
      localStorage.removeItem("orbit_token");
      window.location.replace(`/login?error=${encodeURIComponent(error || "oauth_failed")}`);
    }
  }, [onToken]);
  return <div className="center-state"><RefreshCw className="spin" size={22} /> Completing secure sign-in...</div>;
}

function Login({ error, reason }) {
  return (
    <main className="login-shell">
      <section className="login-panel">
        <p className="project-name">Agentic Workflow Assistant</p>
        <h1>Work with Gmail and Calendar using natural language.</h1>
        <p className="muted">Sign in with Google to get started.</p>
        {error && <div className="notice error"><XCircle size={17} /> Sign-in could not be completed: {error.replaceAll("_", " ")}</div>}
        {!error && reason && <div className="notice error" role="alert"><XCircle size={17} /> {LOGOUT_MESSAGES[reason] || LOGOUT_MESSAGES.unauthenticated}</div>}
        <a className="primary-button" href={`${API}/auth/google/login`}><LogIn size={18} /> Sign in with Google</a>
        <p className="fine-print">Your Google tokens stay encrypted on the server.</p>
      </section>
    </main>
  );
}

function StatusPill({ connected, label }) { return <span className={`status ${connected ? "good" : "off"}`}>{connected ? <CheckCircle2 size={14} /> : <XCircle size={14} />}{label}</span>; }

const STATUS_LABELS = {
  running: "Running",
  needs_clarification: "Needs your input",
  awaiting_approval: "Awaiting approval",
  completed: "Completed",
  succeeded: "Done",
  rejected: "Rejected",
  failed: "Failed",
};
const STATUS_TONE = { completed: "good", succeeded: "good", failed: "bad", rejected: "off", awaiting_approval: "wait", needs_clarification: "wait" };
const statusLabel = (status) => STATUS_LABELS[status] || (status || "unknown").replaceAll("_", " ");
const humanize = (value) => (value || "").replaceAll("_", " ");

function safeUrl(value) {
  return typeof value === "string" && /^https?:\/\//i.test(value) ? value : null;
}

function formatWhen(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return value.length > 10
    ? date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })
    : date.toLocaleDateString([], { dateStyle: "medium" });
}

function formatDuration(ms) {
  if (ms === null || ms === undefined) return "";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
}

function MeetingDetails({ meeting }) {
  const when = [meeting.date_text, meeting.time_text && meeting.time_text !== meeting.date_text ? meeting.time_text : null]
    .filter(Boolean).join(", ");
  const link = safeUrl(meeting.meeting_link);
  const rows = [
    ["Status", (meeting.status || "unclear").replace(/^./, (c) => c.toUpperCase())],
    ["When", when || "Not stated"],
    ["Exact time", meeting.exact_time ? `${meeting.exact_time}${meeting.timezone ? ` (${meeting.timezone})` : ""}` : "Not stated in the email"],
    meeting.location && ["Location", meeting.location],
    meeting.organizer && ["Organizer", meeting.organizer_email && !meeting.organizer.includes(meeting.organizer_email) ? `${meeting.organizer} <${meeting.organizer_email}>` : meeting.organizer],
    meeting.attendees?.length && ["Attendees", meeting.attendees.join(", ")],
  ].filter(Boolean);
  return (
    <dl className="meeting-details">
      {meeting.title && <div className="meeting-title"><CalendarDays size={13} /> {meeting.title}</div>}
      {rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
      {link && <div><dt>Link</dt><dd><a href={link} target="_blank" rel="noreferrer noopener">{link}</a></dd></div>}
    </dl>
  );
}

function EmailCard({ email }) {
  const assessed = typeof email.relevant === "boolean";
  return (
    <li className={`source-card ${assessed && !email.relevant ? "dim" : ""}`}>
      <div className="source-head">
        <Mail size={14} />
        <strong>{email.subject || "(no subject)"}</strong>
        {assessed && <span className={`tag ${email.relevant ? "good" : "off"}`}>{email.relevant ? "Relevant" : "Not relevant"}</span>}
      </div>
      <div className="source-meta">{email.sender || "Unknown sender"}{(email.date || email.internal_date) && ` · ${email.date || formatWhen(email.internal_date)}`}</div>
      {email.evidence && <blockquote className="source-evidence">{email.evidence}</blockquote>}
      {email.meeting && <MeetingDetails meeting={email.meeting} />}
      {email.has_body === false && <p className="source-note">This email has no readable body text.</p>}
      {email.attachments?.length > 0 && <p className="source-note">Attachments: {email.attachments.map((a) => a.filename).join(", ")}</p>}
      {email.body_excerpt && <details><summary>Email content</summary><p className="source-body">{email.body_excerpt}</p></details>}
    </li>
  );
}

function EventCard({ event }) {
  const link = safeUrl(event.html_link);
  return (
    <li className="source-card">
      <div className="source-head"><CalendarDays size={14} /><strong>{event.summary || "(untitled event)"}</strong></div>
      <div className="source-meta">{formatWhen(event.start)}{event.end && ` – ${formatWhen(event.end)}`}</div>
      {event.attendees?.length > 0 && <p className="source-note">Attendees: {event.attendees.join(", ")}</p>}
      {link && <a className="source-link" href={link} target="_blank" rel="noreferrer noopener">Open in Google Calendar</a>}
    </li>
  );
}

function ResponseDetails({ data }) {
  if (!data) return null;
  const emails = Array.isArray(data.messages) ? data.messages : data.message ? [data.message] : [];
  const events = [...(Array.isArray(data.events) ? data.events : []), ...(data.duplicate ? [data.duplicate] : [])];
  if (!emails.length && !events.length) return null;
  const label = emails.length ? `${emails.length} email${emails.length === 1 ? "" : "s"} checked` : `${events.length} event${events.length === 1 ? "" : "s"}`;
  return (
    <details className="response-details" open={emails.some((e) => e.relevant) || events.length <= 3}>
      <summary>{label}</summary>
      <ul className="source-list">
        {emails.map((email) => <EmailCard key={email.id} email={email} />)}
        {events.map((event) => <EventCard key={event.id} event={event} />)}
      </ul>
    </details>
  );
}

function StepIcon({ status }) {
  if (status === "running") return <RefreshCw className="spin" size={12} />;
  if (status === "error" || status === false) return <XCircle size={12} />;
  if (status === "ok" || status === true) return <CheckCircle2 size={12} />;
  return <Circle size={12} />;
}

function stepTone(status) {
  return status === "error" || status === false ? "bad" : status === "running" ? "wait" : "good";
}

function StepList({ steps }) {
  return (
    <ol className="step-list">
      {steps.map((step, i) => (
        <li key={step.key || i} className={`step ${stepTone(step.status)} ${step.kind === "tool" ? "tool" : ""}`}>
          <StepIcon status={step.status} />
          {step.kind === "tool" && <Wrench size={11} />}
          <span className="step-label">{step.label}</span>
          {step.detail && <span className="step-detail">{humanize(step.detail)}</span>}
          {step.ms !== undefined && step.ms !== null && <span className="step-ms">{formatDuration(step.ms)}</span>}
        </li>
      ))}
    </ol>
  );
}

/** Fold a streamed progress/tool event into the live step list: "running" opens a step, "ok"/"error" closes it. */
export function applyProgressEvent(steps, event) {
  if (event.type !== "progress" && event.type !== "tool") return steps;
  const id = event.type === "tool" ? `tool:${event.server}.${event.tool}` : `node:${event.node}`;
  if (event.status === "running") {
    return [...steps, { key: `${id}:${steps.length}`, id, kind: event.type === "tool" ? "tool" : "node", label: event.label, status: "running" }];
  }
  const index = steps.findLastIndex((s) => s.id === id && s.status === "running");
  const done = { status: event.status, ms: event.ms, detail: event.status === "error" ? event.detail : undefined };
  if (index === -1) return [...steps, { key: `${id}:${steps.length}`, id, kind: event.type === "tool" ? "tool" : "node", label: event.label, ...done }];
  return steps.map((s, i) => (i === index ? { ...s, ...done } : s));
}

const NODE_NAMES = {
  load_context: "Load context", select_service: "Select service", recognize_intent: "Recognize intent",
  enhance_context: "Resolve context", validate_parameters: "Validate request", gmail_agent: "Gmail agent",
  calendar_agent: "Calendar agent", mcp_tools: "MCP tools", request_approval: "Request approval",
  generate_response: "Compose answer", persist_turn: "Save conversation",
};

function WorkflowDetails({ meta }) {
  const trace = Array.isArray(meta.trace) ? meta.trace : [];
  if (!trace.length && !meta.request_id) return null;
  const tools = Array.isArray(meta.mcp_calls) ? meta.mcp_calls : [];
  const models = Array.isArray(meta.llm_calls) ? meta.llm_calls : [];
  const total = trace.reduce((sum, step) => sum + (step.ms || 0), 0);
  return (
    <details className="response-details workflow-details">
      <summary>Workflow details{total ? ` · ${formatDuration(total)}` : ""}</summary>
      <dl className="kv">
        {meta.intent && <div><dt>Intent</dt><dd>{humanize(meta.intent)}</dd></div>}
        {meta.service && <div><dt>Service</dt><dd>{meta.service}</dd></div>}
        {models.length > 0 && <div><dt>Models</dt><dd>{models.map((m) => `${m.role}: ${m.model}`).join(", ")}</dd></div>}
        {meta.error?.code && <div><dt>Error</dt><dd>{humanize(meta.error.code)}</dd></div>}
        {meta.request_id && <div><dt>Request ID</dt><dd><code>{meta.request_id}</code></dd></div>}
      </dl>
      {trace.length > 0 && <StepList steps={trace.map((s, i) => ({ key: i, label: NODE_NAMES[s.node] || s.node, status: s.status, ms: s.ms, detail: s.status === "error" ? s.detail : undefined }))} />}
      {tools.length > 0 && (
        <>
          <p className="kv-heading">Tool calls</p>
          <StepList steps={tools.map((t, i) => ({ key: i, kind: "tool", label: `${t.server}.${t.tool}`, status: t.ok, ms: t.ms, detail: t.error }))} />
        </>
      )}
    </details>
  );
}

function AssistantBubble({ message, canDecide, deciding, onDecide, quickReplies, onQuickReply, disabled }) {
  const [copied, setCopied] = useState("");
  const meta = message.meta || {};
  const status = meta.status;
  const failed = status === "failed";

  async function copy() {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopied("Copied");
    } catch {
      setCopied("Copy failed");
    }
    setTimeout(() => setCopied(""), 2000);
  }

  return (
    <article className={`bubble assistant ${failed ? "response-error" : ""} ${canDecide ? "needs-approval" : ""}`}>
      <div className="response-header">
        <h3>{failed ? "Unable to complete request" : "Assistant"}</h3>
        <div className="response-actions">
          {status && <span className={`response-status ${STATUS_TONE[status] || ""}`}>{statusLabel(status)}</span>}
          <button type="button" className="copy-button" onClick={copy} aria-label="Copy response"><Copy size={13} /> {copied || "Copy"}</button>
        </div>
      </div>
      <div className="response-content">{message.content || "No response was returned."}</div>
      <ResponseDetails data={meta.data} />
      {status === "needs_clarification" && !quickReplies && (
        <p className="callout"><HelpCircle size={14} /> Reply below to continue.</p>
      )}
      {canDecide && (
        <div className="approval-actions">
          <p className="callout"><ShieldCheck size={14} /> Nothing is sent or changed until you approve.</p>
          <button type="button" className="send-button" disabled={disabled} onClick={() => onDecide(meta.workflow.id, true)}>
            {deciding ? <RefreshCw className="spin" size={15} /> : <CheckCircle2 size={15} />} {deciding ? "Submitting..." : "Approve"}
          </button>
          <button type="button" className="secondary-button" disabled={disabled} onClick={() => onDecide(meta.workflow.id, false)}>Reject</button>
        </div>
      )}
      {quickReplies && (
        <div className="approval-actions">
          {quickReplies.map((reply) => (
            <button key={reply} type="button" className="secondary-button" disabled={disabled} onClick={() => onQuickReply(reply)}>{reply}</button>
          ))}
        </div>
      )}
      <WorkflowDetails meta={meta} />
    </article>
  );
}

function UserBubble({ message, onRetry, disabled }) {
  return (
    <article className={`bubble user ${message.failed ? "failed" : ""}`}>
      <div>{message.content}</div>
      {message.failed && (
        <p className="bubble-error"><XCircle size={13} /> Not sent: {message.failed}
          {onRetry && <button type="button" className="text-button" disabled={disabled} onClick={() => onRetry(message)}>Retry</button>}
        </p>
      )}
    </article>
  );
}

function LoadingBubble({ steps, recovering }) {
  const current = [...steps].reverse().find((s) => s.status === "running");
  return (
    <article className="bubble assistant response-loading" aria-busy="true">
      <div className="response-header"><h3>Assistant</h3><span><RefreshCw className="spin" size={14} /> {recovering ? "Reconnecting" : current?.label || "Working on it"}</span></div>
      {recovering && <p className="callout recovering"><AlertTriangle size={14} /> {recovering}</p>}
      {steps.length ? <StepList steps={steps} /> : !recovering && <p><Clock size={13} /> Reading your Gmail and Calendar can take up to a minute.</p>}
    </article>
  );
}

function ExecutionDetail({ detail }) {
  const rows = [
    ["Execution ID", <code>{detail.id}</code>],
    ["Request ID", <code>{detail.request_id}</code>],
    detail.workflow_id && ["Approval workflow", <code>{detail.workflow_id}</code>],
    ["Started", formatWhen(detail.created_at)],
    detail.completed_at && ["Finished", formatWhen(detail.completed_at)],
    detail.agent && ["Agent", detail.agent],
    detail.approval_status && ["Approval", humanize(detail.approval_status)],
    detail.action_status && ["Action", humanize(detail.action_status)],
    detail.error_code && ["Error", `${humanize(detail.error_code)}${detail.error_message ? ` — ${detail.error_message}` : ""}`],
  ].filter(Boolean);
  return (
    <div className="execution-detail">
      <dl className="kv">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
      {detail.steps?.length > 0 && (
        <>
          <p className="kv-heading">Workflow steps</p>
          <StepList steps={detail.steps.map((s, i) => ({ key: i, label: NODE_NAMES[s.node] || s.node, status: s.status, ms: s.ms, detail: s.status === "error" ? s.detail : undefined }))} />
        </>
      )}
      {detail.tools?.length > 0 && (
        <>
          <p className="kv-heading">Tool calls</p>
          <StepList steps={detail.tools.map((t, i) => ({ key: i, kind: "tool", label: `${t.server}.${t.tool}${t.phase === "approval" ? " (after approval)" : ""}`, status: t.ok, ms: t.duration_ms, detail: t.error }))} />
        </>
      )}
      {detail.llm_calls?.length > 0 && (
        <>
          <p className="kv-heading">Language model calls</p>
          <StepList steps={detail.llm_calls.map((c, i) => ({ key: i, label: `${humanize(c.role)} · ${c.model}`, status: c.ok, ms: c.duration_ms, detail: c.ok ? (c.total_tokens ? `${c.total_tokens} tokens` : undefined) : c.error }))} />
        </>
      )}
    </div>
  );
}

function ActivityView({ token, onError }) {
  const [items, setItems] = useState(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState("");
  const [open, setOpen] = useState(null);
  const [details, setDetails] = useState({});

  async function load() {
    setLoading(true);
    setFailed("");
    try {
      setItems(await request("/executions?limit=50", {}, token));
    } catch (err) {
      setFailed(describeError(err));
      onError(err, { silent: true });
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  async function toggle(id) {
    if (open === id) { setOpen(null); return; }
    setOpen(id);
    if (details[id]) return;
    try {
      const detail = await request(`/executions/${id}`, {}, token);
      setDetails((current) => ({ ...current, [id]: detail }));
    } catch (err) {
      setDetails((current) => ({ ...current, [id]: { error: describeError(err) } }));
      onError(err, { silent: true });
    }
  }

  return (
    <section className="activity-view" aria-label="Execution history">
      <div className="section-heading"><span>Execution history</span><button className="text-button" onClick={load} disabled={loading}>Refresh</button></div>
      <p className="muted small">Every assistant request, the agents and tools it used, approvals, and the outcome. Only you can see your history.</p>
      {loading && !items ? (
        <div className="center-state"><RefreshCw className="spin" size={22} /> Loading activity...</div>
      ) : failed ? (
        <p className="muted">Activity could not be loaded: {failed}</p>
      ) : !items?.length ? (
        <p className="muted">No requests yet. Ask the assistant something and it will appear here.</p>
      ) : (
        <ul className="activity-list">
          {items.map((item) => (
            <li key={item.id} className="activity-entry">
              <button type="button" className="activity-item" aria-expanded={open === item.id} onClick={() => toggle(item.id)}>
                <div>
                  <strong>{item.request_text || humanize(item.intent) || "Request"}</strong>
                  <span>{formatWhen(item.created_at)}{item.intent ? ` · ${humanize(item.intent)}` : ""}{item.service ? ` · ${item.service}` : ""}{item.tool_count ? ` · ${item.tool_count} tool${item.tool_count === 1 ? "" : "s"}` : ""}{item.duration_ms != null ? ` · ${formatDuration(item.duration_ms)}` : ""}</span>
                </div>
                <span className={`pill ${STATUS_TONE[item.status] || ""}`}>{statusLabel(item.status)}</span>
              </button>
              {open === item.id && (
                details[item.id]?.error ? <p className="muted small">{details[item.id].error}</p>
                  : details[item.id] ? <ExecutionDetail detail={details[item.id]} />
                    : <div className="thread-state"><RefreshCw className="spin" size={14} /> Loading details...</div>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function Dashboard({ token, onLogout }) {
  const [me, setMe] = useState(null);
  const [conversations, setConversations] = useState([]);
  const [loading, setLoading] = useState(true);
  const [prompt, setPrompt] = useState("");
  const [messages, setMessages] = useState([]);
  const [threadLoading, setThreadLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [steps, setSteps] = useState([]);
  const [recovering, setRecovering] = useState("");
  const [deciding, setDeciding] = useState(null);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [tab, setTab] = useState("intent");
  const [selectedConversation, setSelectedConversation] = useState(null);
  const inFlight = useRef(false);
  const activeConversation = useRef(null);
  const threadEnd = useRef(null);
  const inputRef = useRef(null);

  function handleError(err, { silent = false } = {}) {
    if (err.kind === "auth" || err.status === 401) {
      onLogout(err.code === "session_expired" ? "session_expired" : "unauthenticated");
      return;
    }
    if (!silent) setError(describeError(err));
  }

  useEffect(() => {
    // Scroll the page itself to the end so the sticky composer settles below the newest message, not over it.
    if (threadEnd.current) window.scrollTo?.({ top: document.documentElement.scrollHeight, behavior: "smooth" });
  }, [messages, busy, steps.length]);

  async function loadMessages(conversationId) {
    activeConversation.current = conversationId;
    setThreadLoading(true);
    try {
      const detail = await request(`/conversations/${conversationId}`, {}, token);
      if (activeConversation.current === conversationId) setMessages(detail.messages || []);
      return detail.messages || [];
    } catch (err) {
      handleError(err);
      return null;
    } finally {
      if (activeConversation.current === conversationId) setThreadLoading(false);
    }
  }

  function selectConversation(conversationId) {
    if (inFlight.current || conversationId === selectedConversation) return;
    setSelectedConversation(conversationId);
    setMessages([]);
    setError("");
    loadMessages(conversationId);
  }

  // The backend resolves "yesterday", "Friday 3 PM" and calendar day bounds in the account's timezone.
  async function syncTimezone(profile) {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (!zone || zone === profile.user?.timezone) return profile;
    try {
      const user = await request("/auth/me/timezone", { method: "PATCH", body: JSON.stringify({ timezone: zone }) }, token);
      return { ...profile, user };
    } catch (err) {
      if (err.kind === "auth") throw err;
      apiLog("warn", "timezone sync failed", { timezone: zone, status: err.status, request_id: err.requestId });
      return profile;
    }
  }

  async function load() {
    setLoading(true);
    setError("");
    try {
      const [profile, items] = await Promise.all([
        request("/auth/me", {}, token),
        request("/conversations", {}, token),
      ]);
      setMe(await syncTimezone(profile));
      setConversations(items);
      if (items.length) {
        setSelectedConversation(items[0].id);
        loadMessages(items[0].id);
      }
    } catch (err) {
      handleError(err);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  async function ensureConversation() {
    if (selectedConversation) return selectedConversation;
    const item = await request("/conversations", {
      method: "POST",
      body: JSON.stringify({ title: "New workspace" }),
    }, token);
    setConversations((items) => [item, ...items]);
    setSelectedConversation(item.id);
    activeConversation.current = item.id;
    return item.id;
  }

  async function send(text, retryOf = null) {
    const content = text.trim();
    if (!content || inFlight.current) return;
    inFlight.current = true;
    const localId = `local-${Date.now()}`;
    setBusy(true);
    setSteps([]);
    setError("");
    setInfo("");
    setPrompt("");
    setMessages((items) => [...items.filter((m) => m.id !== retryOf), { id: localId, role: "user", content, local: true }]);
    const markNotSent = (err) => {
      setMessages((items) => [...items.filter((m) => m.id !== localId), { id: localId, role: "user", content, local: true, failed: describeError(err) }]);
      setPrompt((current) => current || content);
    };
    let conversationId = null;
    try {
      conversationId = await ensureConversation();
      const turn = await streamTurn(conversationId, content, token, (event) => setSteps((current) => applyProgressEvent(current, event)));
      if (activeConversation.current === conversationId) {
        setMessages((items) => [...items.filter((m) => m.id !== localId), turn.user_message, turn.assistant_message]);
      }
    } catch (err) {
      if (err.kind === "auth") {
        handleError(err);
      } else if (!conversationId || err.kind === "http") {
        // Rejected before the turn started (or the conversation couldn't be created): nothing ran on the server.
        markNotSent(err);
        handleError(err);
      } else {
        await recoverTurn(err, conversationId, content, markNotSent);
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
      setSteps([]);
      setRecovering("");
      inputRef.current?.focus();
    }
  }

  /**
   * The response stream was lost, timed out, or the server reported an error. The turn may still have run, so
   * find its real outcome (never resubmit automatically - an approved action must not run twice).
   */
  async function recoverTurn(err, conversationId, content, markNotSent) {
    let run = null;
    if (err.kind !== "stream_error") {
      setRecovering(`${err.message} Checking whether the assistant finished${err.requestId ? ` (request ID ${err.requestId})` : ""}...`);
      try {
        run = await waitForExecution({ executionId: err.executionId, requestId: err.requestId }, token,
          { maxMs: err.kind === "network" ? 15000 : 240000 });
      } catch (pollErr) {
        if (pollErr.kind === "auth") { handleError(pollErr); return; }
      }
      apiLog(run ? "info" : "warn", "turn recovery finished", { request_id: err.requestId, execution_id: run?.id || err.executionId, kind: err.kind, outcome: run ? run.status : run === null ? "not_received" : "still_running" });
    }
    const saved = await loadMessages(conversationId);
    const savedOnServer = saved?.some((m) => m.role === "user" && m.content === content);
    if (!savedOnServer && (run === null || err.kind === "network" || err.kind === "stream_error")) {
      markNotSent(err);
      setError(describeError(err));
    } else if (err.kind === "stream_error") {
      setError(describeError(err));
    } else if (run) {
      setInfo(`The connection dropped while the assistant was working, so this result was loaded from the server (request ID ${err.requestId}).`);
    } else {
      setError(`${describeError(err)} The assistant may still be working on it: refresh this conversation or check Activity in a minute instead of resending, so it doesn't run twice.`);
    }
  }

  function submit(event) {
    event.preventDefault();
    send(prompt);
  }

  function onPromptKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      send(prompt);
    }
  }

  async function decideWorkflow(workflowId, approve) {
    if (inFlight.current) return;
    inFlight.current = true;
    setDeciding(workflowId);
    setError("");
    const conversationId = selectedConversation;
    try {
      await request(`/workflows/${workflowId}/decision`, {
        method: "POST",
        body: JSON.stringify({ approve }),
        timeoutMs: 120000,
      }, token);
    } catch (err) {
      handleError(err);
    } finally {
      // The backend appends the real outcome (or keeps the request pending) - show exactly that.
      if (conversationId) await loadMessages(conversationId);
      inFlight.current = false;
      setDeciding(null);
    }
  }

  async function createConversation() {
    if (inFlight.current) return;
    try {
      const item = await request("/conversations", {
        method: "POST",
        body: JSON.stringify({ title: "New workspace" }),
      }, token);
      setConversations((items) => [item, ...items]);
      setSelectedConversation(item.id);
      activeConversation.current = item.id;
      setMessages([]);
      setError("");
      inputRef.current?.focus();
    } catch (err) {
      handleError(err);
    }
  }

  if (loading) {
    return <div className="center-state"><RefreshCw className="spin" size={22} /> Loading your workspace...</div>;
  }
  if (!me) {
    return <div className="center-state load-error"><div><p>{error || "Your workspace could not be loaded."}</p><button className="secondary-button" onClick={load}>Try again</button><button className="secondary-button" onClick={onLogout}>Sign in</button></div></div>;
  }

  const { user, connections } = me;
  const decided = new Set(messages
    .filter((m) => m.meta?.workflow?.id && m.meta.status !== "awaiting_approval")
    .map((m) => m.meta.workflow.id));
  const lastMessage = messages[messages.length - 1];
  const locked = busy || deciding !== null;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">Agentic Workflow Assistant</div>
        <button className={`nav-item ${tab === "intent" ? "active" : ""}`} onClick={() => setTab("intent")}><MessageSquare size={17} /> Assistant<ChevronRight size={15} /></button>
        <button className={`nav-item ${tab === "mail" ? "active" : ""}`} onClick={() => setTab("mail")}><Mail size={17} /> Gmail<ChevronRight size={15} /></button>
        <button className={`nav-item ${tab === "calendar" ? "active" : ""}`} onClick={() => setTab("calendar")}><CalendarDays size={17} /> Calendar<ChevronRight size={15} /></button>
        <button className={`nav-item ${tab === "activity" ? "active" : ""}`} onClick={() => { setTab("activity"); setError(""); }}><ActivityIcon size={17} /> Activity<ChevronRight size={15} /></button>
        <div className="side-bottom">
          <div className="avatar">{(user.name || user.email)[0].toUpperCase()}</div>
          <div className="profile"><strong>{user.name || "Connected user"}</strong><span>{user.email}</span></div>
          <button className="icon-button" title="Sign out" onClick={onLogout}><LogIn size={16} /></button>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div><h2>Good to see you{user.name ? `, ${user.name.split(" ")[0]}` : ""}.</h2></div>
          <div className="connections"><StatusPill connected={connections.gmail} label="Gmail" /><StatusPill connected={connections.calendar} label="Calendar" /></div>
        </header>

        {error && <div className="notice error" role="alert"><AlertTriangle size={17} /> <span>{error}</span>
          {tab === "intent" && selectedConversation && !busy && <button type="button" className="text-button" onClick={() => { setError(""); loadMessages(selectedConversation); }}>Refresh conversation</button>}
        </div>}
        {info && !error && <div className="notice info" role="status"><CheckCircle2 size={17} /> {info}</div>}

        {tab === "intent" && (
          <section className="content-grid">
            <div className="assistant-main">
              <section className="thread" aria-label="Conversation" aria-live="polite">
                {threadLoading && messages.length === 0 && (
                  <div className="thread-state"><RefreshCw className="spin" size={16} /> Loading conversation...</div>
                )}
                {!threadLoading && messages.length === 0 && !busy && (
                  <div className="thread-state">
                    <MessageSquare size={18} />
                    <span>Ask about your email or calendar, for example who emailed you about a meeting, or what is on your calendar tomorrow.</span>
                  </div>
                )}
                {messages.map((message) => message.role === "user" ? (
                  <UserBubble key={message.id} message={message} disabled={locked} onRetry={(m) => send(m.content, m.id)} />
                ) : (
                  <AssistantBubble
                    key={message.id}
                    message={message}
                    disabled={locked}
                    deciding={deciding === message.meta?.workflow?.id}
                    canDecide={message.meta?.status === "awaiting_approval" && message.meta?.workflow?.id && !decided.has(message.meta.workflow.id)}
                    onDecide={decideWorkflow}
                    quickReplies={message === lastMessage && message.meta?.status === "needs_clarification" && message.meta?.data?.calendar_offer
                      ? ["Yes, add it to my calendar", "No thanks"] : null}
                    onQuickReply={(reply) => send(reply)}
                  />
                ))}
                {busy && <LoadingBubble steps={steps} recovering={recovering} />}
                <div ref={threadEnd} />
              </section>

              <form onSubmit={submit} className="prompt-box composer">
                <label htmlFor="assistant-prompt">{messages.length ? "Reply or ask a follow-up" : "What would you like to do?"}</label>
                <textarea id="assistant-prompt" ref={inputRef} value={prompt} onChange={(event) => setPrompt(event.target.value)} onKeyDown={onPromptKeyDown}
                  placeholder="Describe a task for Gmail or Calendar..." maxLength={2000} readOnly={busy} aria-describedby="prompt-hint" />
                <div className="prompt-footer">
                  <span id="prompt-hint">Enter to send · Shift+Enter for a new line · {prompt.length} / 2000</span>
                  <button className="send-button" disabled={locked || !prompt.trim()}>{busy ? <RefreshCw className="spin" size={17} /> : <Send size={17} />}{busy ? "Working..." : "Send"}</button>
                </div>
              </form>
            </div>

            <div className="rail">
              <div className="rail-section">
                <div className="section-heading"><span>Conversations</span><button className="text-button" onClick={createConversation} disabled={locked}>+ New</button></div>
                {conversations.length === 0 ? (
                  <p className="muted small">No conversations yet.</p>
                ) : conversations.slice(0, 5).map((item) => (
                  <button className={`conversation ${selectedConversation === item.id ? "active" : ""}`} key={item.id} disabled={locked} onClick={() => selectConversation(item.id)}><MessageSquare size={16} /><span>{item.title}</span><ChevronRight size={14} /></button>
                ))}
              </div>
            </div>
          </section>
        )}

        {tab === "activity" && <ActivityView token={token} onError={handleError} />}

        {tab !== "intent" && tab !== "activity" && (
          <section className="empty-view">
            <div className="empty-icon">{tab === "mail" ? <Mail size={28} /> : <CalendarDays size={28} />}</div>
            <h1>{tab === "mail" ? "Gmail" : "Calendar"}</h1>
            <p className="muted">Describe a task in Assistant to work with {tab === "mail" ? "Gmail" : "Google Calendar"}.</p>
            <button className="secondary-button" onClick={() => setTab("intent")}>Go to Assistant <ChevronRight size={16} /></button>
          </section>
        )}
      </main>
    </div>
  );
}

export default function App() {
  const [token, setToken] = useState(() => localStorage.getItem("orbit_token"));
  const [logoutReason, setLogoutReason] = useState(null);
  const error = new URLSearchParams(window.location.search).get("error");

  function updateToken(value) {
    localStorage.setItem("orbit_token", value);
    setLogoutReason(null);
    setToken(value);
  }

  function logout(reason = null) {
    localStorage.removeItem("orbit_token");
    setLogoutReason(typeof reason === "string" ? reason : null);
    setToken(null);
  }

  if (window.location.pathname === "/auth/callback") {
    return <AuthCallback onToken={updateToken} />;
  }

  return token ? <Dashboard token={token} onLogout={logout} /> : <Login error={error} reason={logoutReason} />;
}
