// Same-origin "/api" in production builds (Vercel routes it to the FastAPI service); a local backend in development.
export const API = import.meta.env.VITE_API_URL || (import.meta.env.DEV ? "http://localhost:8000/api" : "/api");

// The server sends a heartbeat every 5 s while a turn runs; this much silence means the connection is dead.
export const STREAM_STALL_MS = 20000;

/**
 * kind: "network" (no response at all), "timeout", "auth" (401; code "session_expired" when the JWT expired),
 * "http" (any other non-2xx), "stream_interrupted" (the connection broke after the turn started),
 * "stream_incomplete" (the stream closed without a result), "stream_error" (the server reported a failure).
 */
export class ApiError extends Error {
  constructor(message, { kind, status, code, requestId, executionId } = {}) {
    super(message);
    this.kind = kind;
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.executionId = executionId;
  }
}

export function newRequestId() {
  const id = globalThis.crypto?.randomUUID?.() || `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`;
  return id.replaceAll("-", "").slice(0, 32);
}

/** Structured browser-console log line; the request_id matches the backend's JSON logs and Vercel log search. */
export function apiLog(level, event, fields = {}) {
  try {
    (console[level] || console.log)(`[api] ${event}`, fields);
  } catch { /* Logging must never break a request. */ }
}

async function errorFromResponse(response, requestId) {
  let detail = `Request failed (HTTP ${response.status})`;
  let code;
  try {
    const body = await response.json();
    const apiDetail = body.detail;
    detail = typeof apiDetail === "string" ? apiDetail : apiDetail?.message || apiDetail?.code || detail;
    code = typeof apiDetail === "object" ? apiDetail?.code : undefined;
  } catch { /* Keep the status-based message for an empty or non-JSON response. */ }
  if (response.status === 401) {
    return new ApiError(code === "session_expired" ? "Your session has expired. Please sign in again." : "You are signed out. Please sign in again.",
      { kind: "auth", status: 401, code: code || "unauthenticated", requestId });
  }
  return new ApiError(detail, { kind: "http", status: response.status, code, requestId: response.headers?.get?.("x-request-id") || requestId });
}

function headers(token, requestId) {
  return { "Content-Type": "application/json", "X-Request-ID": requestId, ...(token ? { Authorization: `Bearer ${token}` } : {}) };
}

function transportError(error, { method, requestId, timedOut }) {
  if (error instanceof ApiError) return error;
  if (error?.name === "AbortError" || timedOut) {
    return new ApiError(method === "POST"
      ? "The request timed out. The server may still be processing it."
      : "The request timed out. Please try again.", { kind: "timeout", requestId });
  }
  return new ApiError("Couldn't reach the server. Check your internet connection and try again.", { kind: "network", requestId });
}

export async function request(path, options = {}, token) {
  const { timeoutMs: configuredTimeout, ...fetchOptions } = options;
  const method = fetchOptions.method || "GET";
  const timeoutMs = configuredTimeout ?? (method === "POST" ? 180000 : 30000);
  const requestId = newRequestId();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  const started = Date.now();
  try {
    const response = await fetch(`${API}${path}`, {
      ...fetchOptions,
      signal: controller.signal,
      headers: { ...headers(token, requestId), ...(fetchOptions.headers || {}) },
    });
    if (!response.ok) throw await errorFromResponse(response, requestId);
    const body = response.status === 204 ? null : await response.json();
    apiLog("debug", "request completed", { method, path, status: response.status, request_id: requestId, duration_ms: Date.now() - started });
    return body;
  } catch (error) {
    const err = transportError(error, { method, requestId });
    apiLog(err.kind === "auth" ? "info" : "warn", "request failed", { method, path, kind: err.kind, status: err.status, request_id: err.requestId, duration_ms: Date.now() - started, error: err.message });
    throw err;
  } finally {
    clearTimeout(timeout);
  }
}

/**
 * Run one assistant turn through the streaming endpoint. Each NDJSON event (started, progress, tool, heartbeat) is
 * passed to `onEvent`; resolves with the persisted turn from the final `result` event. Every rejection is an
 * ApiError carrying the request id, and - once the server has started the turn - its execution id, so the caller
 * can recover the outcome instead of resubmitting.
 */
export async function streamTurn(conversationId, content, token, onEvent = () => {}, { timeoutMs = 300000, stallMs = STREAM_STALL_MS } = {}) {
  const path = `/conversations/${conversationId}/messages/stream`;
  const requestId = newRequestId();
  const controller = new AbortController();
  const state = { started: Date.now(), lastByte: Date.now(), headers: false, events: 0, executionId: undefined, stalled: false, timedOut: false };
  const timeout = setTimeout(() => { state.timedOut = true; controller.abort(); }, timeoutMs);
  const watchdog = setInterval(() => {
    if (state.headers && Date.now() - state.lastByte > stallMs) { state.stalled = true; controller.abort(); }
  }, Math.min(2000, stallMs));
  apiLog("debug", "stream initiated", { method: "POST", path, request_id: requestId });
  try {
    const response = await fetch(`${API}${path}`, {
      method: "POST", signal: controller.signal, headers: headers(token, requestId), body: JSON.stringify({ content }),
    });
    if (!response.ok) throw await errorFromResponse(response, requestId);
    state.headers = true;
    state.lastByte = Date.now();
    apiLog("debug", "stream response received", { path, status: response.status, request_id: requestId, ttfb_ms: Date.now() - state.started });
    let result = null;
    let failure = null;
    const handle = (line) => {
      if (!line.trim()) return;
      let event;
      try { event = JSON.parse(line); } catch { return; }
      if (state.events++ === 0) apiLog("debug", "stream first event", { request_id: requestId, type: event.type, after_ms: Date.now() - state.started });
      if (event.type === "started") state.executionId = event.execution_id || state.executionId;
      if (event.type === "result") result = event.turn;
      else if (event.type === "error") failure = event;
      else onEvent(event);
    };
    if (response.body?.getReader) {
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        state.lastByte = Date.now();
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();
        lines.forEach(handle);
      }
      handle(buffer + decoder.decode());
    } else {
      (await response.text()).split("\n").forEach(handle);
    }
    if (failure) {
      throw new ApiError(failure.message || "The assistant could not complete the request.",
        { kind: "stream_error", code: failure.code, requestId: failure.request_id || requestId, executionId: state.executionId });
    }
    if (!result) {
      throw new ApiError("The connection closed before the assistant finished.",
        { kind: "stream_incomplete", requestId, executionId: state.executionId });
    }
    apiLog("debug", "stream completed", { request_id: requestId, events: state.events, duration_ms: Date.now() - state.started });
    return result;
  } catch (error) {
    let err;
    if (error instanceof ApiError) err = error;
    else if (state.headers && !state.timedOut) {
      // The server accepted the turn; only the connection carrying its progress was lost.
      err = new ApiError(state.stalled
        ? "The connection to the server stalled while the assistant was working."
        : "The connection to the server was interrupted while the assistant was working.",
      { kind: "stream_interrupted", requestId, executionId: state.executionId });
    } else err = transportError(error, { method: "POST", requestId, timedOut: state.timedOut });
    apiLog(err.kind === "auth" ? "info" : "warn", "stream failed", { path, kind: err.kind, status: err.status, request_id: err.requestId, execution_id: err.executionId, events: state.events, after_ms: Date.now() - state.started, error: err.message });
    throw err;
  } finally {
    clearTimeout(timeout);
    clearInterval(watchdog);
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * After a lost stream, wait for the server-side outcome of the turn: by execution id when the `started` event
 * arrived, otherwise by the request id the client sent. Resolves with the execution summary once it is no longer
 * running, `null` when the server has no record of the request, or `undefined` if it is still running at `maxMs`.
 */
export async function waitForExecution({ executionId, requestId }, token, { intervalMs = 3000, maxMs = 240000, onPoll } = {}) {
  const deadline = Date.now() + maxMs;
  let lookups = 0;
  while (Date.now() < deadline) {
    try {
      const run = executionId
        ? await request(`/executions/${executionId}`, {}, token)
        : (await request(`/executions?limit=1&request_id=${encodeURIComponent(requestId)}`, {}, token))[0];
      lookups += 1;
      if (run && run.status !== "running") return run;
      if (run) executionId = run.id;
      // A request the server never received leaves no record; give it a few seconds to appear.
      if (!run && lookups >= 3) return null;
    } catch (error) {
      if (error.kind === "auth") throw error;
    }
    onPoll?.();
    await sleep(intervalMs);
  }
  return undefined;
}
