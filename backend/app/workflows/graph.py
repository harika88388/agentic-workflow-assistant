"""LangGraph orchestration of one conversation turn.

    load_context -> select_service -> recognize_intent -> enhance_context -> validate_parameters
        -> (clarification | gmail_agent | calendar_agent)
    gmail_agent / calendar_agent <-> mcp_tools        (loop until the agent has what it needs)
        -> request_approval (consequential actions) | result | clarification | error
    -> generate_response -> persist_turn

Any node failure is caught, recorded in `error` and routed straight to generate_response, so the user always gets
an honest answer and nothing is reported as done unless an MCP tool confirmed it.
"""
import logging
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from app.agents.base import AgentContext, AgentOutcome
from app.agents.service_selector import ServiceSelection
from app.database.models import Message
from app.mcp.errors import MCPToolError
from app.schemas.intent import QUESTIONS, IntentResult
from app.services import workflow_service
from app.services.llm.base import LLMConfigError, LLMProviderError, LLMResponseError
from app.services.llm.router import LLMRole
from app.workflows.state import WorkflowDeps, WorkflowState

log = logging.getLogger(__name__)
MAX_AGENT_ITERATIONS = 8
SERVICE_QUESTIONS = {
    "gmail": "What would you like me to do in Gmail? I can send, search, read, reply to, summarize or draft emails.",
    "calendar": "What would you like me to do in your calendar? I can list, create, reschedule or cancel events, "
                "or check your availability.",
}
DEFAULT_QUESTION = "I can help with Gmail and Google Calendar. What would you like to do?"


def classify_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, MCPToolError):
        return exc.code, exc.message
    if isinstance(exc, LLMConfigError):
        return "llm_not_configured", str(exc)
    if isinstance(exc, LLMProviderError):
        return "llm_unavailable", str(exc)
    if isinstance(exc, LLMResponseError):
        return "llm_invalid_response", "The language model did not return a usable answer."
    if isinstance(exc, ValueError):
        return "invalid_request", str(exc)
    return "internal_error", "Something went wrong while processing your request."


def node(name: str) -> Callable:
    """Wrap a node: inject deps, record its execution status in `trace`, and turn exceptions into `error`."""
    def deco(fn: Callable[[WorkflowState, WorkflowDeps], dict[str, Any]]):
        # No functools.wraps: LangGraph inspects the signature to decide whether to pass `config`.
        def wrapper(state: WorkflowState, config: RunnableConfig) -> dict[str, Any]:
            deps: WorkflowDeps = config["configurable"]["deps"]
            try:
                update = fn(state, deps) or {}
            except Exception as exc:  # noqa: BLE001 - every failure becomes an explicit, reported error
                code, message = classify_error(exc)
                if code == "internal_error":
                    log.exception("workflow node %s failed", name)
                else:
                    log.warning("workflow node %s failed: %s", name, code)
                return {"status": "failed", "error": {"code": code, "message": message, "node": name},
                        "trace": [{"node": name, "status": "error", "detail": code}]}
            detail = update.pop("detail", None)
            return {**update, "trace": [{"node": name, "status": "ok", **({"detail": detail} if detail else {})}]}
        wrapper.__name__ = name
        return wrapper
    return deco


# ---------------- nodes ----------------
@node("load_context")
def load_context(state: WorkflowState, d: WorkflowDeps) -> dict[str, Any]:
    d.ctx = d.context_agent.load(d.db, d.conversation, reference_date=d.now.date(), timezone=d.tz.key,
                                 exclude_message_id=d.user_message_id)
    pending = d.ctx.pending.get("intent") if d.ctx.pending else None
    return {"status": "running", "tool_results": {}, "iterations": 0,
            "detail": f"{len(d.ctx.history)} earlier messages; pending={pending or 'none'}"}


@node("select_service")
def select_service(state: WorkflowState, d: WorkflowDeps) -> dict[str, Any]:
    assert d.ctx is not None
    sel = d.selector.select(state["message"], pending_service=(d.ctx.pending or {}).get("service"))
    return {"selection": sel.to_dict(), "detail": f"{sel.service} via {sel.source}"}


@node("recognize_intent")
def recognize_intent(state: WorkflowState, d: WorkflowDeps) -> dict[str, Any]:
    assert d.ctx is not None
    hint = state["selection"]["service"]
    d.llm_calls.append(d.llm.call_record(LLMRole.INTENT))
    ir = d.intent_agent.recognize(state["message"], reference_date=d.now.date(), timezone=d.tz.key,
                                  conversation_context=d.context_agent.render_for_intent(d.ctx),
                                  service_hint=hint if hint != "unknown" else None)
    return {"intent": ir.model_dump(), "detail": f"{ir.intent} (follow_up={ir.is_follow_up})"}


@node("enhance_context")
def enhance_context(state: WorkflowState, d: WorkflowDeps) -> dict[str, Any]:
    assert d.ctx is not None
    ir = IntentResult.model_validate(state["intent"])
    req = d.context_agent.enhance(ir, state["message"], d.ctx)
    effective = ir.model_copy(update={"intent": req.intent, "service": req.service})
    final = d.selector.reconcile(ServiceSelection(**state["selection"]), effective,
                                 continued_service=(d.ctx.pending or {}).get("service") if req.continued_pending else None)
    return {"request": req.to_dict(), "service": final.service, "selection": final.to_dict(),
            "detail": "; ".join(req.notes) or "no context needed"}


@node("validate_parameters")
def validate_parameters(state: WorkflowState, d: WorkflowDeps) -> dict[str, Any]:
    req = dict(state["request"])
    if req.get("abandon"):
        return {"outcome": AgentOutcome("result", message="Okay, I've dropped that request.").to_dict(),
                "missing": [], "detail": "pending request abandoned", "request": req | {"clear_pending": True}}
    if req["intent"] == "unknown":
        ir = IntentResult.model_validate(state["intent"])
        question = SERVICE_QUESTIONS.get(state.get("service", "unknown")) if ir.clarification_question in (None, DEFAULT_QUESTION) \
            else ir.clarification_question
        return {"outcome": AgentOutcome("clarify", message=question or DEFAULT_QUESTION).to_dict(),
                "missing": [], "detail": "intent unknown"}

    checked = IntentResult(service=req["service"], intent=req["intent"], entities=req["entities"]).enforce_requirements()
    entities = {k: v for k, v in checked.entities.model_dump().items() if v not in (None, [], "")}
    req["entities"] = entities
    missing = list(checked.missing_parameters)
    if req.get("date_unresolved"):
        question = f"I couldn't work out which date \"{req.get('date_text')}\" is. Which date do you mean?"
        missing = [m for m in missing if m != "date"] + ["date"]
    elif missing:
        question = " ".join(QUESTIONS[m] for m in missing)
    elif req.get("llm_question"):
        question = req["llm_question"]
        missing = ["clarification"]
    else:
        return {"request": req, "missing": [], "detail": "all required parameters present"}
    outcome = AgentOutcome("clarify", message=question, pending={"entities": entities, "awaiting": missing})
    return {"request": req, "missing": missing, "outcome": outcome.to_dict(), "detail": f"missing {missing}"}


def _run_agent(state: WorkflowState, d: WorkflowDeps) -> dict[str, Any]:
    assert d.ctx is not None
    if state.get("iterations", 0) >= MAX_AGENT_ITERATIONS:
        raise RuntimeError("agent did not converge")
    req = state["request"]
    agent = d.agents[req["intent"]]
    if agent.service != state["service"]:
        raise ValueError(f"{req['intent']} is not a {state['service']} operation")
    actx = AgentContext(intent=req["intent"], entities=dict(req["entities"]), message=state["message"], tz=d.tz,
                        now=d.now, memory=d.ctx.memory, pending=d.ctx.pending,
                        tool_results=dict(state.get("tool_results") or {}), user_name=d.user.name or "")
    outcome = agent.step(actx)
    update: dict[str, Any] = {"outcome": outcome.to_dict(), "detail": f"{type(agent).__name__} -> {outcome.kind}"}
    if outcome.kind == "tool_calls":
        update["iterations"] = state.get("iterations", 0) + 1
    if outcome.kind == "error":
        update |= {"status": "failed", "error": {"code": outcome.error_code or "operation_failed",
                                                 "message": outcome.message, "node": type(agent).__name__}}
    return update


gmail_agent = node("gmail_agent")(_run_agent)
calendar_agent = node("calendar_agent")(_run_agent)


@node("mcp_tools")
def mcp_tools(state: WorkflowState, d: WorkflowDeps) -> dict[str, Any]:
    results = dict(state.get("tool_results") or {})
    calls = state["outcome"]["tool_calls"]
    for call in calls:
        results[call["key"]] = d.gateway.call_tool(call["server"], call["tool"], call["arguments"])
    return {"tool_results": results, "detail": ", ".join(f"{c['server']}.{c['tool']}" for c in calls)}


@node("request_approval")
def request_approval(state: WorkflowState, d: WorkflowDeps) -> dict[str, Any]:
    p = state["outcome"]["proposal"]
    execution = workflow_service.propose(d.db, d.user, conversation_id=d.conversation.id, service=p["service"],
                                         intent=p["intent"], request=p["request"],
                                         idempotency_key=f"chat:{state['message_id']}")
    return {"workflow": {"id": str(execution.id), "status": execution.status, "service": execution.service,
                         "intent": execution.intent, "request": execution.request},
            "status": "awaiting_approval", "detail": f"workflow {execution.id} awaiting approval"}


_ERROR_PREFIX = {
    "llm_not_configured": "The language model isn't configured, so I can't process requests yet",
    "llm_unavailable": "The language model is unavailable right now",
    "google_reconnect_required": "I need access to your Google account again. Please reconnect it",
    "mcp_connection_failed": "I couldn't reach the Google tools service",
}


@node("generate_response")
def generate_response(state: WorkflowState, d: WorkflowDeps) -> dict[str, Any]:
    if state.get("error"):
        err = state["error"]
        prefix = _ERROR_PREFIX.get(err["code"], "I couldn't complete that")
        return {"status": "failed", "response": f"{prefix}: {err['message']}", "detail": err["code"]}
    outcome = state.get("outcome") or {}
    kind = outcome.get("kind")
    if kind == "proposal":
        text = f"{outcome['proposal']['preview']}\n\nApprove to go ahead, or reject to cancel. Nothing happens until you approve."
        return {"status": "awaiting_approval", "response": text, "detail": "approval requested"}
    if kind == "clarify":
        return {"status": "needs_clarification", "response": outcome["message"], "detail": "clarification"}
    return {"status": "completed", "response": outcome.get("message") or "Done.", "detail": "result"}


@node("persist_turn")
def persist_turn(state: WorkflowState, d: WorkflowDeps) -> dict[str, Any]:
    assert d.ctx is not None
    outcome = state.get("outcome") or {}
    req = state.get("request") or {}
    status = state["status"]
    memory = dict(outcome.get("memory") or {})
    pending: dict[str, Any] | None = None
    if status == "needs_clarification" and req.get("intent", "unknown") != "unknown":
        p = outcome.get("pending") or {}
        pending = {"intent": req["intent"], "service": req["service"], "entities": p.get("entities", req.get("entities", {})),
                   "awaiting": p.get("awaiting", []), "question": state.get("response")}
        if p.get("candidates"):
            pending |= {"candidates": p["candidates"], "candidate_type": p.get("candidate_type", "event")}
        if pending["entities"].get("recipient_email"):
            memory.setdefault("last_contact_email", pending["entities"]["recipient_email"])
    elif status == "failed" and not req.get("clear_pending"):
        pending = d.ctx.pending  # a failure must not lose the conversation's unfinished request
    d.context_agent.save(d.db, d.ctx, memory_updates=memory, pending=pending)

    meta = {
        "status": status, "service": state.get("service"), "intent": req.get("intent"),
        "entities": req.get("entities"), "missing": state.get("missing"), "selection": state.get("selection"),
        "context_notes": req.get("notes"), "workflow": state.get("workflow"), "data": outcome.get("data"),
        "error": state.get("error"), "trace": state.get("trace", []), "llm_calls": list(d.llm_calls),
        "mcp_calls": list(d.gateway.calls), "mcp_transport": d.gateway.transport,
    }
    msg = Message(conversation_id=d.conversation.id, role="assistant", content=state["response"], meta=meta)
    d.db.add(msg)
    d.db.commit()
    return {"assistant_message_id": str(msg.id), "detail": "saved"}


# ---------------- routing ----------------
def _unless_error(next_node: str) -> Callable[[WorkflowState], str]:
    return lambda s: "generate_response" if s.get("error") else next_node


def route_after_validation(s: WorkflowState) -> str:
    if s.get("error") or s.get("outcome"):
        return "generate_response"  # error, clarification or abandoned request
    return {"gmail": "gmail_agent", "calendar": "calendar_agent"}.get(s.get("service", ""), "generate_response")


def route_after_agent(s: WorkflowState) -> str:
    if s.get("error"):
        return "generate_response"
    kind = s["outcome"]["kind"]
    return {"tool_calls": "mcp_tools", "proposal": "request_approval"}.get(kind, "generate_response")


def route_after_tools(s: WorkflowState) -> str:
    if s.get("error"):
        return "generate_response"
    return "gmail_agent" if s["service"] == "gmail" else "calendar_agent"


def build_graph():
    g = StateGraph(WorkflowState)
    for name, fn in [("load_context", load_context), ("select_service", select_service),
                     ("recognize_intent", recognize_intent), ("enhance_context", enhance_context),
                     ("validate_parameters", validate_parameters), ("gmail_agent", gmail_agent),
                     ("calendar_agent", calendar_agent), ("mcp_tools", mcp_tools),
                     ("request_approval", request_approval), ("generate_response", generate_response),
                     ("persist_turn", persist_turn)]:
        g.add_node(name, fn)
    g.add_edge(START, "load_context")
    chain = ["load_context", "select_service", "recognize_intent", "enhance_context", "validate_parameters"]
    for a, b in zip(chain, chain[1:]):
        g.add_conditional_edges(a, _unless_error(b), [b, "generate_response"])
    g.add_conditional_edges("validate_parameters", route_after_validation,
                            ["gmail_agent", "calendar_agent", "generate_response"])
    for agent in ("gmail_agent", "calendar_agent"):
        g.add_conditional_edges(agent, route_after_agent, ["mcp_tools", "request_approval", "generate_response"])
    g.add_conditional_edges("mcp_tools", route_after_tools, ["gmail_agent", "calendar_agent", "generate_response"])
    g.add_edge("request_approval", "generate_response")
    g.add_edge("generate_response", "persist_turn")
    g.add_edge("persist_turn", END)
    return g.compile()


@lru_cache
def get_graph():
    return build_graph()
