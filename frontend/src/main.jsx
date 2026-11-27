import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { Activity as ActivityIcon, CalendarDays, CheckCircle2, ChevronRight, Copy, LogIn, Mail, MessageSquare, RefreshCw, Send, XCircle } from "lucide-react";
import "./styles.css";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000/api";

async function request(path, options = {}, token) {
  const { timeoutMs: configuredTimeout, ...fetchOptions } = options;
  const timeoutMs = configuredTimeout ?? (fetchOptions.method === "POST" ? 180000 : 30000);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${API}${path}`, {
      ...fetchOptions,
      signal: controller.signal,
      headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(fetchOptions.headers || {}) },
    });
    if (!response.ok) {
      let detail = `Request failed (${response.status})`;
      try {
        const body = await response.json();
        const apiDetail = body.detail;
        detail = typeof apiDetail === "string" ? apiDetail : apiDetail?.message || apiDetail?.code || detail;
      } catch { /* Keep the status-based message for an empty or non-JSON response. */ }
      const error = new Error(detail);
      error.status = response.status;
      throw error;
    }
    return response.status === 204 ? null : await response.json();
  } catch (error) {
    if (error.status !== undefined) throw error;
    if (error.name === "AbortError") {
      const message = fetchOptions.method === "POST"
        ? "The request timed out. The backend may still be processing it; check Activity or this conversation before retrying."
        : "The request timed out. Check that the backend is running and try again.";
      throw new Error(message);
    }
    if (error instanceof TypeError) {
      throw new Error("Unable to reach the backend. Check that it is running and try again.");
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

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

function Login({ error }) {
  return (
    <main className="login-shell">
      <section className="login-panel">
        <p className="project-name">Agentic Workflow Assistant</p>
        <h1>Work with Gmail and Calendar using natural language.</h1>
        <p className="muted">Sign in with Google to get started.</p>
        {error && <div className="notice error"><XCircle size={17} /> Sign-in could not be completed: {error.replaceAll("_", " ")}</div>}
        <a className="primary-button" href={`${API}/auth/google/login`}><LogIn size={18} /> Sign in with Google</a>
        <p className="fine-print">Your Google tokens stay encrypted on the server.</p>
      </section>
    </main>
  );
}

function StatusPill({ connected, label }) { return <span className={`status ${connected ? "good" : "off"}`}>{connected ? <CheckCircle2 size={14} /> : <XCircle size={14} />}{label}</span>; }

function Dashboard({ token, onLogout }) {
  const [me, setMe] = useState(null);
  const [conversations, setConversations] = useState([]);
  const [activities, setActivities] = useState([]);
  const [activityLoading, setActivityLoading] = useState(false);
  const [loading, setLoading] = useState(true);
  const [prompt, setPrompt] = useState("");
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [tab, setTab] = useState("intent");
  const [selectedConversation, setSelectedConversation] = useState(null);
  const [lastPrompt, setLastPrompt] = useState("");
  const [copyStatus, setCopyStatus] = useState("");

  function handleError(err) {
    if (err.status === 401) {
      onLogout();
      return;
    }
    setError(err.message);
  }

  async function load() {
    setLoading(true);
    setError("");
    try {
      const [profile, items] = await Promise.all([
        request("/auth/me", {}, token),
        request("/conversations", {}, token),
      ]);
      setMe(profile);
      setConversations(items);
      if (items.length) setSelectedConversation(items[0].id);
    } catch (err) {
      handleError(err);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  async function loadActivities() {
    setActivityLoading(true);
    setError("");
    try {
      setActivities(await request("/workflows", {}, token));
    } catch (err) {
      handleError(err);
    } finally {
      setActivityLoading(false);
    }
  }

  async function ensureConversation() {
    if (selectedConversation) return selectedConversation;
    const item = await request("/conversations", {
      method: "POST",
      body: JSON.stringify({ title: "New workspace" }),
    }, token);
    setConversations((items) => [item, ...items]);
    setSelectedConversation(item.id);
    return item.id;
  }

  async function analyze(event) {
    event.preventDefault();
    if (!prompt.trim()) return;
    const submittedPrompt = prompt.trim();
    setLastPrompt(submittedPrompt);
    setBusy(true);
    setError("");
    setResult(null);
    setCopyStatus("");
    try {
      const conversationId = await ensureConversation();
      const turn = await request(`/conversations/${conversationId}/messages`, {
        method: "POST",
        body: JSON.stringify({ content: submittedPrompt }),
      }, token);
      setResult(turn);
      setPrompt("");
    } catch (err) {
      handleError(err);
    } finally {
      setBusy(false);
    }
  }

  async function decideWorkflow(approve) {
    if (!result?.workflow?.id) return;
    setBusy(true);
    setError("");
    try {
      const workflow = await request(`/workflows/${result.workflow.id}/decision`, {
        method: "POST",
        body: JSON.stringify({ approve }),
      }, token);
      setResult({
        status: workflow.status,
        workflow,
        assistant_message: {
          content: approve
            ? `Approved. Workflow status: ${workflow.status}.`
            : "Action rejected.",
        },
      });
    } catch (err) {
      handleError(err);
    } finally {
      setBusy(false);
    }
  }

  async function createConversation() {
    try {
      const item = await request("/conversations", {
        method: "POST",
        body: JSON.stringify({ title: "New workspace" }),
      }, token);
      setConversations((items) => [item, ...items]);
      setSelectedConversation(item.id);
    } catch (err) {
      handleError(err);
    }
  }

  async function copyResponse() {
    const responseText = result?.assistant_message?.content;
    if (!responseText) return;
    try {
      await navigator.clipboard.writeText(responseText);
      setCopyStatus("Response copied");
    } catch {
      setCopyStatus("Copy failed. Select and copy the response instead.");
    }
  }

  if (loading) {
    return <div className="center-state"><RefreshCw className="spin" size={22} /> Loading your workspace...</div>;
  }
  if (!me) {
    return <div className="center-state load-error"><div><p>{error || "Your workspace could not be loaded."}</p><button className="secondary-button" onClick={load}>Try again</button><button className="secondary-button" onClick={onLogout}>Sign in</button></div></div>;
  }

  const { user, connections } = me;
  const responseFailed = result?.status === "failed";

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">Agentic Workflow Assistant</div>
        <button className={`nav-item ${tab === "intent" ? "active" : ""}`} onClick={() => setTab("intent")}><MessageSquare size={17} /> Assistant<ChevronRight size={15} /></button>
        <button className={`nav-item ${tab === "mail" ? "active" : ""}`} onClick={() => setTab("mail")}><Mail size={17} /> Gmail<ChevronRight size={15} /></button>
        <button className={`nav-item ${tab === "calendar" ? "active" : ""}`} onClick={() => setTab("calendar")}><CalendarDays size={17} /> Calendar<ChevronRight size={15} /></button>
        <button className={`nav-item ${tab === "activity" ? "active" : ""}`} onClick={() => { setTab("activity"); loadActivities(); }}><ActivityIcon size={17} /> Activity<ChevronRight size={15} /></button>
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

        {error && <div className="notice error"><XCircle size={17} /> {error}</div>}

        {tab === "intent" && (
          <section className="content-grid">
            <div className="assistant-main">
              <form onSubmit={analyze} className="prompt-box">
                <label htmlFor="assistant-prompt">What would you like to do?</label>
                <textarea id="assistant-prompt" value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="Describe a task for Gmail or Calendar..." maxLength={2000} />
                <div className="prompt-footer">
                  <span>{prompt.length} / 2000</span>
                  <button className="send-button" disabled={busy || !prompt.trim()}>{busy ? <RefreshCw className="spin" size={17} /> : <Send size={17} />}{busy ? "Running workflow..." : "Run workflow"}</button>
                </div>
              </form>

              {lastPrompt && (
                <section className="user-message" aria-label="Your request">
                  <h3>Your request</h3>
                  <div>{lastPrompt}</div>
                </section>
              )}

              {busy && (
                <section className="response-panel response-loading" aria-live="polite">
                  <div className="response-header"><h3>Assistant response</h3><span><RefreshCw className="spin" size={16} /> Processing</span></div>
                  <p>The assistant is working on your request...</p>
                </section>
              )}

              {result && (
                <section className={`response-panel ${responseFailed ? "response-error" : ""}`}>
                  <div className="response-header">
                    <h3>{responseFailed ? "Unable to complete request" : "Assistant response"}</h3>
                    <div className="response-actions">
                      <span className="response-status">{result.status.replaceAll("_", " ")}</span>
                      <button type="button" className="copy-button" onClick={copyResponse} aria-label="Copy response">
                        <Copy size={15} /> Copy
                      </button>
                    </div>
                  </div>
                  <div className="response-content">{result.assistant_message?.content || "No response was returned."}</div>
                  {copyStatus && <p className="copy-status" role="status">{copyStatus}</p>}
                  {result.status === "awaiting_approval" && result.workflow?.id && (
                    <div className="approval-actions">
                      <button type="button" className="send-button" disabled={busy} onClick={() => decideWorkflow(true)}>Approve</button>
                      <button type="button" className="secondary-button" disabled={busy} onClick={() => decideWorkflow(false)}>Reject</button>
                    </div>
                  )}
                </section>
              )}
            </div>

            <div className="rail">
              <div className="rail-section">
                <div className="section-heading"><span>Conversations</span><button className="text-button" onClick={createConversation}>+ New</button></div>
                {conversations.length === 0 ? (
                  <p className="muted small">No conversations yet.</p>
                ) : conversations.slice(0, 5).map((item) => (
                  <button className={`conversation ${selectedConversation === item.id ? "active" : ""}`} key={item.id} onClick={() => setSelectedConversation(item.id)}><MessageSquare size={16} /><span>{item.title}</span><ChevronRight size={14} /></button>
                ))}
              </div>
            </div>
          </section>
        )}

        {tab === "activity" && (
          <section className="activity-view">
            <div className="section-heading"><span>Recent workflow activity</span><button className="text-button" onClick={loadActivities} disabled={activityLoading}>Refresh</button></div>
            {activityLoading ? (
              <div className="center-state"><RefreshCw className="spin" size={22} /> Loading activity...</div>
            ) : error ? (
              <p className="muted">Activity could not be loaded. Try refreshing.</p>
            ) : activities.length === 0 ? (
              <p className="muted">No workflow executions yet. Actions requiring approval and their outcomes will appear here.</p>
            ) : (
              <div className="activity-list">
                {activities.map((item) => (
                  <div className="activity-item" key={item.id}>
                    <div><strong>{(item.intent || "Workflow").replaceAll("_", " ")}</strong><span>{item.service || "Assistant"} · {new Date(item.created_at).toLocaleString()}</span></div>
                    <span className={`activity-status ${["succeeded", "rejected"].includes(item.status) ? "good" : "off"}`}>{item.status.replaceAll("_", " ")}</span>
                  </div>
                ))}
              </div>
            )}
          </section>
        )}

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

function App() {
  const [token, setToken] = useState(() => localStorage.getItem("orbit_token"));
  const error = new URLSearchParams(window.location.search).get("error");

  function updateToken(value) {
    localStorage.setItem("orbit_token", value);
    setToken(value);
  }

  function logout() {
    localStorage.removeItem("orbit_token");
    setToken(null);
  }

  if (window.location.pathname === "/auth/callback") {
    return <AuthCallback onToken={updateToken} />;
  }

  return token ? <Dashboard token={token} onLogout={logout} /> : <Login error={error} />;
}

createRoot(document.getElementById("root")).render(<App />);
