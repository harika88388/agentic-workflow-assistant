import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { CalendarDays, CheckCircle2, ChevronRight, LogIn, Mail, MessageSquare, RefreshCw, Send, Sparkles, XCircle } from "lucide-react";
import "./styles.css";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000/api";

async function request(path, options = {}, token) {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(options.headers || {}) },
  });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try { detail = (await response.json()).detail || detail; } catch { /* empty response */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function AuthCallback({ onToken }) {
  useEffect(() => {
    const token = new URLSearchParams(window.location.hash.slice(1)).get("token");
    const error = new URLSearchParams(window.location.search).get("error");
    if (token) { window.history.replaceState({}, "", "/"); onToken(token); }
    else if (error) window.history.replaceState({}, "", `/?error=${encodeURIComponent(error)}`);
  }, [onToken]);
  return <div className="center-state"><RefreshCw className="spin" size={22} /> Completing secure sign-in...</div>;
}

function Login({ error }) {
  return <main className="login-shell"><div className="login-panel"><div className="mark"><Sparkles size={20} /></div><p className="eyebrow">ORBIT DESK</p><h1>Your workday, in one clear view.</h1><p className="muted">A private command center for Gmail and Google Calendar. Connect your account to begin.</p>{error && <div className="notice error"><XCircle size={17} /> Sign-in could not be completed: {error.replaceAll("_", " ")}</div>}<a className="primary-button" href={`${API}/auth/google/login`}><LogIn size={18} /> Continue with Google</a><p className="fine-print">Your Google tokens stay encrypted on the server.</p></div><div className="login-art"><div className="orbit orbit-one" /><div className="orbit orbit-two" /><div className="art-copy"><span>PRIVATE BY DEFAULT</span><strong>Ask. Review.<br />Act with intent.</strong></div></div></main>;
}

function StatusPill({ connected, label }) { return <span className={`status ${connected ? "good" : "off"}`}>{connected ? <CheckCircle2 size={14} /> : <XCircle size={14} />}{label}</span>; }

function Dashboard({ token, onLogout }) {
  const [me, setMe] = useState(null);
  const [conversations, setConversations] = useState([]);
  const [prompt, setPrompt] = useState("");
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [tab, setTab] = useState("intent");

  async function load() {
    try { setError(""); const [profile, items] = await Promise.all([request("/auth/me", {}, token), request("/conversations", {}, token)]); setMe(profile); setConversations(items); }
    catch (err) { setError(err.message); }
  }
  useEffect(() => { load(); }, []);

  async function analyze(event) {
    event.preventDefault(); if (!prompt.trim()) return;
    setBusy(true); setError(""); setResult(null);
    try { setResult(await request("/intent/analyze", { method: "POST", body: JSON.stringify({ message: prompt }) }, token)); }
    catch (err) { setError(err.message); } finally { setBusy(false); }
  }
  async function createConversation() {
    try { const item = await request("/conversations", { method: "POST", body: JSON.stringify({ title: "New workspace" }) }, token); setConversations([item, ...conversations]); }
    catch (err) { setError(err.message); }
  }
  if (!me) return <div className="center-state"><RefreshCw className="spin" size={22} /> Loading your workspace...</div>;
  const { user, connections } = me;
  return <div className="app-shell"><aside className="sidebar"><div className="brand"><div className="brand-mark"><Sparkles size={18} /></div><span>Orbit Desk</span></div><div className="side-label">Workspace</div><button className={`nav-item ${tab === "intent" ? "active" : ""}`} onClick={() => setTab("intent")}><MessageSquare size={17} /> Intent desk<ChevronRight size={15} /></button><button className={`nav-item ${tab === "mail" ? "active" : ""}`} onClick={() => setTab("mail")}><Mail size={17} /> Gmail<ChevronRight size={15} /></button><button className={`nav-item ${tab === "calendar" ? "active" : ""}`} onClick={() => setTab("calendar")}><CalendarDays size={17} /> Calendar<ChevronRight size={15} /></button><div className="side-bottom"><div className="avatar">{(user.name || user.email)[0].toUpperCase()}</div><div className="profile"><strong>{user.name || "Connected user"}</strong><span>{user.email}</span></div><button className="icon-button" title="Sign out" onClick={onLogout}><LogIn size={16} /></button></div></aside><main className="main"><header className="topbar"><div><p className="eyebrow">TUESDAY, NOVEMBER 24</p><h2>Good to see you{user.name ? `, ${user.name.split(" ")[0]}` : ""}.</h2></div><div className="connections"><StatusPill connected={connections.gmail} label="Gmail" /><StatusPill connected={connections.calendar} label="Calendar" /></div></header>{error && <div className="notice error"><XCircle size={17} /> {error}</div>}{tab === "intent" && <section className="content-grid"><div className="hero-card"><div className="card-kicker"><Sparkles size={16} /> Intent desk</div><h1>What should we<br /><em>untangle</em> today?</h1><p className="muted">Describe a Gmail or Calendar task in plain language. Orbit will classify it and show what it needs before anything acts.</p><form onSubmit={analyze} className="prompt-box"><textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="Try: Move my meeting with Rahul to Friday at 3 PM" maxLength={2000} /><div className="prompt-footer"><span>{prompt.length} / 2000</span><button className="send-button" disabled={busy || !prompt.trim()}>{busy ? <RefreshCw className="spin" size={17} /> : <Send size={17} />} Analyze request</button></div></form>{result && <div className="result"><div className="result-top"><span className="result-label">CLASSIFICATION</span><span className={`confidence ${result.requires_clarification ? "warn" : ""}`}>{result.requires_clarification ? "Needs clarification" : "Ready for review"}</span></div><h3>{result.intent || "Unclear request"}</h3>{result.clarification_question && <p>{result.clarification_question}</p>}<div className="chips">{(result.missing_parameters || []).map((item) => <span key={item}>{item.replaceAll("_", " ")}</span>)}</div></div>}</div><div className="rail"><div className="rail-section"><div className="section-heading"><span>Recent spaces</span><button className="text-button" onClick={createConversation}>+ New</button></div>{conversations.length === 0 ? <p className="muted small">No spaces yet. Create one to keep a thread of work together.</p> : conversations.slice(0, 5).map((item) => <div className="conversation" key={item.id}><MessageSquare size={16} /><span>{item.title}</span><ChevronRight size={14} /></div>)}</div><div className="rail-section status-card"><div className="section-heading"><span>System status</span><CheckCircle2 size={17} /></div><p>API is connected and ready for authenticated requests.</p></div></div></section>}{tab !== "intent" && <section className="empty-view"><div className="empty-icon">{tab === "mail" ? <Mail size={28} /> : <CalendarDays size={28} />}</div><h1>{tab === "mail" ? "Gmail" : "Calendar"} is connected.</h1><p className="muted">Read-only integration views are available through the API. Action workflows will arrive with the approval phase.</p><a className="secondary-button" href="http://localhost:8000/docs" target="_blank" rel="noreferrer">Open API workspace <ChevronRight size={16} /></a></section>}</main></div>;
}

function App() {
  const [token, setToken] = useState(() => localStorage.getItem("orbit_token"));
  const error = new URLSearchParams(window.location.search).get("error");
  function updateToken(value) { localStorage.setItem("orbit_token", value); setToken(value); }
  function logout() { localStorage.removeItem("orbit_token"); setToken(null); }
  if (window.location.pathname === "/auth/callback") return <AuthCallback onToken={updateToken} />;
  return token ? <Dashboard token={token} onLogout={logout} /> : <Login error={error} />;
}

createRoot(document.getElementById("root")).render(<App />);
