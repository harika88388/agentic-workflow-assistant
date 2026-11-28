// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Dashboard, applyProgressEvent, describeError } from "./App.jsx";
import { streamTurn, waitForExecution } from "./api.js";

const encoder = new TextEncoder();
const ME = { user: { id: "u1", email: "me@example.com", name: "Ada Lovelace" }, connections: { gmail: true, calendar: true } };
const CONVERSATION = { id: "c1", title: "New workspace" };

const json = (body, status = 200, headers = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });

function stream() {
  let controller;
  const body = new ReadableStream({ start(c) { controller = c; } });
  return {
    response: new Response(body, { status: 200, headers: { "Content-Type": "application/x-ndjson", "X-Request-ID": "req-123" } }),
    push: (event) => controller.enqueue(encoder.encode(`${JSON.stringify(event)}\n`)),
    close: () => controller.close(),
  };
}

const ndjson = (...events) => {
  const s = stream();
  events.forEach(s.push);
  s.close();
  return s.response;
};

function assistant(id, content, meta) {
  return { id, role: "assistant", content, meta, created_at: "2026-11-24T10:00:00Z" };
}

function turn(userText, assistantMessage) {
  return { status: assistantMessage.meta.status, user_message: { id: `u-${assistantMessage.id}`, role: "user", content: userText, meta: {} },
    assistant_message: assistantMessage, workflow: assistantMessage.meta.workflow || null, execution_id: "e1" };
}

const MEETING_ANSWER = assistant("a1", "Yes. aa3corner@gmail.com emailed you that a meeting is scheduled for tonight; no exact time is given.", {
  status: "needs_clarification", intent: "search_emails", service: "gmail", request_id: "req-123",
  data: { calendar_offer: { title: "Meeting" }, messages: [{ id: "m1", subject: "You are gay", sender: "aa3corner@gmail.com", relevant: true,
    meeting: { status: "scheduled", date_text: "tonight", exact_time: null } }] },
  trace: [{ node: "recognize_intent", status: "ok", ms: 3 }, { node: "mcp_tools", status: "ok", ms: 120 }],
  mcp_calls: [{ server: "gmail", tool: "search_emails", ok: true, ms: 80 }, { server: "gmail", tool: "read_email", ok: true, ms: 40 }],
  llm_calls: [{ role: "email_answer", provider: "openrouter", model: "test-model" }],
});

let routes;
let calls;

function mockApi(extra = {}) {
  routes = { "GET /auth/me": () => json(ME), "GET /conversations": () => json([]), "POST /conversations": () => json(CONVERSATION, 201), ...extra };
  calls = [];
  globalThis.fetch = vi.fn(async (url, init = {}) => {
    const path = String(url).replace(/^.*?\/api/, "");
    const key = `${init.method || "GET"} ${path}`;
    calls.push({ key, body: init.body ? JSON.parse(init.body) : undefined, headers: init.headers || {} });
    const handler = routes[key];
    return handler ? handler(init) : json({ detail: "Not found" }, 404);
  });
}

function renderDashboard(onLogout = vi.fn()) {
  render(<Dashboard token="test-jwt" onLogout={onLogout} />);
  return onLogout;
}

async function ask(text) {
  const box = await screen.findByLabelText(/what would you like to do|reply or ask/i);
  fireEvent.change(box, { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: /send/i }));
}

beforeEach(() => { window.scrollTo = vi.fn(); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("applyProgressEvent", () => {
  it("opens a step on running and closes the matching one on completion", () => {
    let steps = applyProgressEvent([], { type: "progress", node: "gmail_agent", status: "running", label: "Gmail agent" });
    steps = applyProgressEvent(steps, { type: "tool", server: "gmail", tool: "search_emails", status: "running", label: "Searching Gmail" });
    steps = applyProgressEvent(steps, { type: "tool", server: "gmail", tool: "search_emails", status: "ok", ms: 80, label: "Searching Gmail" });
    steps = applyProgressEvent(steps, { type: "heartbeat" });
    expect(steps.map((s) => [s.label, s.status, s.ms])).toEqual([["Gmail agent", "running", undefined], ["Searching Gmail", "ok", 80]]);
  });
});

describe("streamTurn", () => {
  it("parses NDJSON split across chunks and resolves with the final turn", async () => {
    const result = turn("hi", MEETING_ANSWER);
    const text = [{ type: "started", request_id: "req-123" }, { type: "progress", node: "load_context", status: "running", label: "Loading" },
      { type: "heartbeat" }, { type: "result", turn: result }].map((e) => JSON.stringify(e)).join("\n") + "\n";
    const body = new ReadableStream({ start(c) { c.enqueue(encoder.encode(text.slice(0, 25))); c.enqueue(encoder.encode(text.slice(25))); c.close(); } });
    globalThis.fetch = vi.fn(async () => new Response(body, { status: 200 }));
    const events = [];
    await expect(streamTurn("c1", "hi", "tok", (e) => events.push(e.type))).resolves.toEqual(result);
    expect(events).toEqual(["started", "progress", "heartbeat"]);
    const [url, init] = globalThis.fetch.mock.calls[0];
    expect(url).toMatch(/\/conversations\/c1\/messages\/stream$/);
    expect(init.headers.Authorization).toBe("Bearer tok");
  });

  it("rejects with the server error and its request ID", async () => {
    globalThis.fetch = vi.fn(async () => ndjson({ type: "started", request_id: "req-9" },
      { type: "error", code: "internal_error", message: "Something went wrong while processing your request.", request_id: "req-9" }));
    await expect(streamTurn("c1", "hi", "tok")).rejects.toMatchObject({ kind: "stream_error", code: "internal_error", requestId: "req-9", message: /something went wrong/i });
  });

  it("reports a stream that ends without a result instead of hanging", async () => {
    globalThis.fetch = vi.fn(async () => ndjson({ type: "started", request_id: "req-1", execution_id: "e7" }));
    await expect(streamTurn("c1", "hi", "tok")).rejects.toMatchObject({ kind: "stream_incomplete", executionId: "e7" });
  });

  it("surfaces HTTP errors with their status and request ID", async () => {
    globalThis.fetch = vi.fn(async () => json({ detail: "Conversation not found" }, 404, { "X-Request-ID": "req-404" }));
    await expect(streamTurn("c1", "hi", "tok")).rejects.toMatchObject({ kind: "http", status: 404, message: "Conversation not found", requestId: "req-404" });
  });

  it("sends its own request ID so a lost request can still be found in the logs", async () => {
    globalThis.fetch = vi.fn(async () => { throw new TypeError("Failed to fetch"); });
    const err = await streamTurn("c1", "hi", "tok").catch((e) => e);
    const sentId = globalThis.fetch.mock.calls[0][1].headers["X-Request-ID"];
    expect(sentId).toMatch(/^[0-9a-f]{32}$/);
    expect(err).toMatchObject({ kind: "network", requestId: sentId, message: /couldn't reach the server/i });
  });

  it("classifies a connection lost mid-stream as interrupted, keeping the execution ID", async () => {
    const body = new ReadableStream({ start(c) {
      c.enqueue(encoder.encode(`${JSON.stringify({ type: "started", request_id: "x", execution_id: "e1" })}\n`));
      setTimeout(() => c.error(new TypeError("network error")), 5);
    } });
    globalThis.fetch = vi.fn(async () => new Response(body, { status: 200 }));
    await expect(streamTurn("c1", "hi", "tok")).rejects.toMatchObject({ kind: "stream_interrupted", executionId: "e1", message: /interrupted/i });
  });

  it("gives up on a stalled stream instead of waiting for the browser's network timeout", async () => {
    globalThis.fetch = vi.fn(async (url, init) => {
      const body = new ReadableStream({ start(c) {
        c.enqueue(encoder.encode(`${JSON.stringify({ type: "started", execution_id: "e2" })}\n`));
        init.signal.addEventListener("abort", () => c.error(new DOMException("Aborted", "AbortError")));
      } });
      return new Response(body, { status: 200 });
    });
    await expect(streamTurn("c1", "hi", "tok", () => {}, { stallMs: 60 })).rejects.toMatchObject({ kind: "stream_interrupted", executionId: "e2", message: /stalled/i });
  });

  it("distinguishes an expired session", async () => {
    globalThis.fetch = vi.fn(async () => json({ detail: { code: "session_expired", message: "expired" } }, 401));
    await expect(streamTurn("c1", "hi", "tok")).rejects.toMatchObject({ kind: "auth", status: 401, code: "session_expired" });
  });
});

describe("waitForExecution", () => {
  it("polls until the turn is no longer running", async () => {
    const statuses = ["running", "completed"];
    globalThis.fetch = vi.fn(async () => json({ id: "e1", status: statuses.shift() }));
    await expect(waitForExecution({ executionId: "e1" }, "tok", { intervalMs: 1 })).resolves.toMatchObject({ status: "completed" });
    expect(String(globalThis.fetch.mock.calls[0][0])).toMatch(/\/executions\/e1$/);
  });

  it("finds a turn by request ID and reports when the server never received it", async () => {
    globalThis.fetch = vi.fn(async () => json([]));
    await expect(waitForExecution({ requestId: "abc123" }, "tok", { intervalMs: 1 })).resolves.toBeNull();
    expect(String(globalThis.fetch.mock.calls[0][0])).toMatch(/\/executions\?limit=1&request_id=abc123$/);
  });
});

describe("describeError", () => {
  it("includes the HTTP status and request ID when known", () => {
    expect(describeError({ message: "Boom", status: 502, requestId: "r1" })).toBe("Boom (HTTP 502 · request ID r1)");
    expect(describeError({ message: "Offline" })).toBe("Offline");
  });
});

describe("Assistant conversation", () => {
  it("shows the user message, live progress, then the grounded answer with follow-up options", async () => {
    const live = stream();
    mockApi({ "POST /conversations/c1/messages/stream": () => live.response });
    renderDashboard();
    await ask("Did I get any mails from aa3corner for a meeting scheduled tonight?");

    expect(await screen.findByText("Did I get any mails from aa3corner for a meeting scheduled tonight?")).toBeTruthy();
    expect(screen.getByText("Working on it")).toBeTruthy();
    expect(screen.getByRole("button", { name: /working/i }).disabled).toBe(true);

    live.push({ type: "started", request_id: "req-123" });
    live.push({ type: "progress", node: "gmail_agent", status: "running", label: "Gmail agent working" });
    live.push({ type: "tool", server: "gmail", tool: "search_emails", status: "running", label: "Searching Gmail" });
    expect((await screen.findAllByText("Searching Gmail")).length).toBeGreaterThan(0);

    live.push({ type: "result", turn: turn("Did I get any mails from aa3corner for a meeting scheduled tonight?", MEETING_ANSWER) });
    live.close();

    expect(await screen.findByText(/no exact time is given/)).toBeTruthy();
    expect(screen.queryByText("Working on it")).toBeNull();
    expect(screen.getByText("Needs your input")).toBeTruthy();
    expect(screen.getByText("Not stated in the email")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Yes, add it to my calendar" })).toBeTruthy();
    expect(screen.getByText("req-123")).toBeTruthy();
    expect(screen.getByText("gmail.read_email")).toBeTruthy();

    const streamCall = calls.find((c) => c.key === "POST /conversations/c1/messages/stream");
    expect(streamCall.body).toEqual({ content: "Did I get any mails from aa3corner for a meeting scheduled tonight?" });
    expect(streamCall.headers.Authorization).toBe("Bearer test-jwt");
  });

  it("sends a quick reply as the next message", async () => {
    mockApi({ "POST /conversations/c1/messages/stream": () => ndjson({ type: "result", turn: turn("q", MEETING_ANSWER) }) });
    renderDashboard();
    await ask("q");
    fireEvent.click(await screen.findByRole("button", { name: "Yes, add it to my calendar" }));
    await waitFor(() => expect(calls.filter((c) => c.key.endsWith("/stream")).map((c) => c.body.content)).toEqual(["q", "Yes, add it to my calendar"]));
  });

  it("asks for approval and only reloads the real outcome after the decision", async () => {
    const proposal = assistant("a2", "I'll create “Meeting” tonight at 8 PM. Approve?", {
      status: "awaiting_approval", workflow: { id: "w1", status: "awaiting_approval" }, intent: "create_event", trace: [] });
    const created = assistant("a3", "Created “Meeting” on your calendar.", { status: "succeeded", workflow: { id: "w1", status: "succeeded" } });
    mockApi({
      "POST /conversations/c1/messages/stream": () => ndjson({ type: "result", turn: turn("8 PM", proposal) }),
      "POST /workflows/w1/decision": () => json({ id: "w1", status: "succeeded" }),
      "GET /conversations/c1": () => json({ ...CONVERSATION, messages: [proposal.meta && { id: "u-a2", role: "user", content: "8 PM", meta: {} }, proposal, created] }),
    });
    renderDashboard();
    await ask("8 PM");
    expect(await screen.findByText("Awaiting approval")).toBeTruthy();
    expect(screen.getByText(/nothing is sent or changed until you approve/i)).toBeTruthy();
    expect(calls.some((c) => c.key.startsWith("POST /workflows"))).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(await screen.findByText("Created “Meeting” on your calendar.")).toBeTruthy();
    expect(calls.find((c) => c.key === "POST /workflows/w1/decision").body).toEqual({ approve: true });
    expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
  });

  it("shows a failed turn returned by the workflow", async () => {
    const failed = assistant("a4", "The language model is unavailable right now. Please try again shortly.",
      { status: "failed", error: { code: "llm_unavailable" }, trace: [{ node: "recognize_intent", status: "error", detail: "llm_unavailable", ms: 5 }] });
    mockApi({ "POST /conversations/c1/messages/stream": () => ndjson({ type: "result", turn: turn("hello", failed) }) });
    renderDashboard();
    await ask("hello");
    expect(await screen.findByText("Unable to complete request")).toBeTruthy();
    expect(screen.getByText("Failed")).toBeTruthy();
  });

  it("reports a server-side stream error with its request ID and shows what was saved", async () => {
    mockApi({
      "POST /conversations/c1/messages/stream": () => ndjson({ type: "started", request_id: "req-err" },
        { type: "error", code: "internal_error", message: "Something went wrong while processing your request.", request_id: "req-err" }),
      "GET /conversations/c1": () => json({ ...CONVERSATION, messages: [{ id: "u1", role: "user", content: "hello", meta: {} }] }),
    });
    renderDashboard();
    await ask("hello");
    expect((await screen.findByRole("alert")).textContent).toMatch(/something went wrong.*req-err/i);
    await waitFor(() => expect(calls.some((c) => c.key === "GET /conversations/c1")).toBe(true));
    expect(screen.queryByText(/not sent/i)).toBeNull();
  });

  it("keeps a message that was rejected before processing and offers a retry", async () => {
    mockApi({ "POST /conversations/c1/messages/stream": () => json({ detail: "Message is too long" }, 422) });
    renderDashboard();
    await ask("hello");
    expect(await screen.findByText(/not sent: message is too long/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(screen.getByLabelText(/what would you like to do|reply or ask/i).value).toBe("hello");
  });

  it("signs out when the session has expired", async () => {
    mockApi({ "GET /auth/me": () => json({ detail: { code: "session_expired", message: "expired" } }, 401) });
    const onLogout = renderDashboard();
    await waitFor(() => expect(onLogout).toHaveBeenCalledWith("session_expired"));
  });

  it("syncs the browser timezone to the account only when it differs", async () => {
    vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({ timeZone: "Asia/Kolkata" });
    mockApi({
      "GET /auth/me": () => json({ ...ME, user: { ...ME.user, timezone: "UTC" } }),
      "PATCH /auth/me/timezone": (init) => json({ ...ME.user, ...JSON.parse(init.body) }),
    });
    renderDashboard();
    await waitFor(() => expect(calls.find((c) => c.key === "PATCH /auth/me/timezone")?.body).toEqual({ timezone: "Asia/Kolkata" }));
    cleanup();

    mockApi({ "GET /auth/me": () => json({ ...ME, user: { ...ME.user, timezone: "Asia/Kolkata" } }) });
    renderDashboard();
    await waitFor(() => expect(calls.map((c) => c.key)).toContain("GET /conversations"));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(calls.some((c) => c.key === "PATCH /auth/me/timezone")).toBe(false);
  });

  it("recovers the saved answer when the connection drops mid-turn, without resending", async () => {
    const saved = assistant("a9", "You have no matching events on Wednesday, 25 November 2026.", { status: "completed", trace: [] });
    mockApi({
      "POST /conversations/c1/messages/stream": () => {
        const body = new ReadableStream({ start(c) {
          c.enqueue(encoder.encode(`${JSON.stringify({ type: "started", request_id: "req-drop", execution_id: "e1" })}\n`));
          setTimeout(() => c.error(new TypeError("network error")), 5);
        } });
        return new Response(body, { status: 200, headers: { "X-Request-ID": "req-drop" } });
      },
      "GET /executions/e1": () => json({ id: "e1", status: "completed", request_id: "req-drop" }),
      "GET /conversations/c1": () => json({ ...CONVERSATION, messages: [{ id: "u9", role: "user", content: "What meetings do I have tomorrow?", meta: {} }, saved] }),
    });
    renderDashboard();
    await ask("What meetings do I have tomorrow?");
    expect(await screen.findByText(saved.content)).toBeTruthy();
    expect((await screen.findByRole("status")).textContent).toMatch(/connection dropped.*loaded from the server/i);
    expect(screen.queryByText(/not sent/i)).toBeNull();
    expect(screen.queryByText(/unable to reach the backend/i)).toBeNull();
    expect(calls.filter((c) => c.key.endsWith("/stream"))).toHaveLength(1);
    expect(calls.map((c) => c.key)).toContain("GET /executions/e1");
  });

  it("marks a message as not sent, with its request ID, when the server never received it", async () => {
    mockApi({
      "POST /conversations/c1/messages/stream": () => { throw new TypeError("Failed to fetch"); },
      "GET /conversations/c1": () => json({ ...CONVERSATION, messages: [] }),
    });
    const realFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async (url, init = {}) => (String(url).includes("/executions?") ? json([]) : realFetch(url, init)));
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      renderDashboard();
      await ask("hello");
      await vi.advanceTimersByTimeAsync(10000);
      const failed = await screen.findByText(/not sent: couldn't reach the server/i);
      expect(failed.textContent).toMatch(/request ID [0-9a-f]{32}/);
      expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("Activity tab", () => {
  it("lists execution history and expands one run's details", async () => {
    const summary = { id: "e1", conversation_id: "c1", request_id: "req-123", request_text: "Did I get any mails from aa3corner?",
      status: "completed", intent: "search_emails", service: "gmail", agent: "EmailSearchAgent", approval_status: null,
      action_status: null, workflow_id: null, error_code: null, duration_ms: 2400, tool_count: 2, created_at: "2026-11-24T10:00:00Z", completed_at: "2026-11-24T10:00:02Z" };
    const detail = { ...summary, user_message_id: "u1", assistant_message_id: "a1", error_message: null,
      steps: [{ node: "recognize_intent", status: "ok", ms: 3 }],
      tools: [{ server: "gmail", tool: "search_emails", ok: true, duration_ms: 80, phase: "turn" }],
      llm_calls: [{ role: "email_answer", model: "test-model", ok: true, duration_ms: 900, total_tokens: 1234 }] };
    mockApi({ "GET /executions?limit=50": () => json([summary]), "GET /executions/e1": () => json(detail) });
    renderDashboard();
    fireEvent.click(await screen.findByRole("button", { name: /activity/i }));

    const row = await screen.findByRole("button", { name: /did i get any mails from aa3corner/i });
    expect(row.textContent).toMatch(/Completed/);
    expect(row.textContent).toMatch(/2 tools/);
    fireEvent.click(row);
    expect(await screen.findByText("Execution ID")).toBeTruthy();
    expect(screen.getByText("req-123")).toBeTruthy();
    expect(screen.getByText("EmailSearchAgent")).toBeTruthy();
    expect(screen.getByText("gmail.search_emails")).toBeTruthy();
    expect(screen.getByText("1234 tokens")).toBeTruthy();
  });

  it("shows an empty state", async () => {
    mockApi({ "GET /executions?limit=50": () => json([]) });
    renderDashboard();
    fireEvent.click(await screen.findByRole("button", { name: /activity/i }));
    expect(await screen.findByText(/no requests yet/i)).toBeTruthy();
  });
});
