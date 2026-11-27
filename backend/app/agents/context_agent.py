"""Context Enhancement Agent: completes a follow-up message using THIS conversation's history and memory.

Deterministic on purpose: the Intent Recognition Agent already sees the recent conversation; this agent merges its
output with the structured per-conversation memory (SessionContext) so that entities survive across turns,
pronouns and ordinals ("him", "that meeting", "the latest one") resolve to concrete ids, and relative dates are
resolved in the user's timezone. Nothing is read from other conversations.
"""
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.dates import extract_time, resolve_date
from app.core.validation import is_valid_email
from app.database.models import Conversation, Message, SessionContext
from app.schemas.intent import CALENDAR_INTENTS, INTENT_TO_SERVICE, Entities, IntentResult

HISTORY_LIMIT = 10
HISTORY_MESSAGE_CHARS = 400
MEMORY_LIST_LIMIT = 10

_PRONOUN_PERSON = re.compile(r"\b(him|her|them|he|she|they)\b", re.I)
_EMAIL_REF = re.compile(
    r"\b(it|that|this|same|latest|last|newest|most recent|first|second|third|oldest|previous)\b", re.I)
_TIME_SPAN = re.compile(r"\b(last|past|previous|this)\s+(week|month|year|few \w+|couple of \w+|\d+ \w+)\b", re.I)
_EVENT_REF = re.compile(r"\b(it|that|this|(that|this|the|same) (meeting|event|call|appointment|one))\b", re.I)
_EMAIL_DETAIL_FOLLOWUP = re.compile(
    r"\bwho sent (?:it|that|this)|\bwho is the sender\b|\bwhat time\b|\bwhat date\b|"
    r"\bwhen (?:is|was|did|does)\b|\bexact time\b|\bdid it mention\b",
    re.I,
)
_ABANDON = re.compile(r"^\s*(never ?mind|forget (it|that)|cancel that|stop|nothing|no thanks?)\b", re.I)
_ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3, "4th": 3, "fifth": 4}
_STOP = {"the", "a", "an", "one", "with", "my", "meeting", "event", "email", "mail", "message", "about", "that",
         "this", "please", "on", "at", "for", "to", "of", "and", "it", "is", "i", "mean"}


def _tokens(text: str | None) -> set[str]:
    return {t for t in re.findall(r"\w+", (text or "").casefold()) if t not in _STOP}


@dataclass
class ConversationContext:
    conversation_id: uuid.UUID
    history: list[dict[str, str]]
    memory: dict[str, Any]
    pending: dict[str, Any] | None
    reference_date: date
    timezone: str


@dataclass
class EnhancedRequest:
    service: str
    intent: str
    entities: dict[str, Any]
    continued_pending: bool = False
    abandon: bool = False
    date_text: str | None = None
    date_unresolved: bool = False
    llm_question: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def select_candidate(message: str, candidates: list[dict[str, Any]], label_key: str = "summary") -> dict | None:
    """Pick one candidate from a list the assistant just showed: by ordinal/number or by unique title overlap."""
    if not candidates:
        return None
    text = message.casefold()
    for word, idx in _ORDINALS.items():
        if re.search(rf"\b{word}\b", text) and idx < len(candidates):
            return candidates[idx]
    if re.search(r"\b(last|latest)\b", text) and label_key == "summary":
        return candidates[-1]
    m = re.fullmatch(r"\s*(?:number|option|#)?\s*(\d)\s*[.!]?\s*", text)
    if m and 1 <= int(m.group(1)) <= len(candidates):
        return candidates[int(m.group(1)) - 1]
    words = _tokens(message)
    if not words:
        return None
    scored = sorted(((len(words & _tokens(c.get(label_key))) / max(1, len(_tokens(c.get(label_key)))), i)
                     for i, c in enumerate(candidates)), reverse=True)
    best, idx = scored[0]
    if best > 0 and (len(scored) == 1 or scored[1][0] < best):
        return candidates[idx]
    return None


def _pick_email(message: str, emails: list[dict[str, Any]]) -> dict | None:
    """'the latest one' = most recent; 'first/second' = position in the list shown; 'oldest' = least recent."""
    if not emails:
        return None
    text = message.casefold()
    by_date = sorted(emails, key=lambda e: e.get("internal_date") or "", reverse=True)
    if re.search(r"\b(latest|newest|most recent|last)\b", text):
        return by_date[0]
    if re.search(r"\boldest\b", text):
        return by_date[-1]
    chosen = select_candidate(message, emails, label_key="subject")
    if chosen:
        return chosen
    return emails[0] if len(emails) == 1 else None


class ContextEnhancementAgent:
    # ---------- retrieval (PostgreSQL) ----------
    def load(self, db: Session, conversation: Conversation, *, reference_date: date, timezone: str,
             exclude_message_id: uuid.UUID | None = None) -> ConversationContext:
        stmt = (select(Message).where(Message.conversation_id == conversation.id)
                .order_by(Message.created_at.desc()).limit(HISTORY_LIMIT + 1))
        rows = [m for m in db.scalars(stmt) if m.id != exclude_message_id][:HISTORY_LIMIT]
        history = [{"role": m.role, "content": m.content[:HISTORY_MESSAGE_CHARS]} for m in reversed(rows)]
        sc = db.scalar(select(SessionContext).where(SessionContext.conversation_id == conversation.id))
        return ConversationContext(
            conversation_id=conversation.id, history=history,
            memory=dict(sc.entities or {}) if sc else {}, pending=dict(sc.pending_intent) if sc and sc.pending_intent else None,
            reference_date=reference_date, timezone=timezone,
        )

    def render_for_intent(self, ctx: ConversationContext) -> str:
        """Conversation context for the intent agent. The tail is kept when truncated, so facts go last."""
        lines = [f"{h['role']}: {h['content']}" for h in ctx.history]
        facts: list[str] = []
        if ctx.pending:
            known = ", ".join(f"{k}={v}" for k, v in (ctx.pending.get("entities") or {}).items() if v)
            facts.append(f"- pending request: {ctx.pending.get('intent')} ({known or 'no details yet'}); "
                         f"waiting for: {', '.join(ctx.pending.get('awaiting') or []) or 'confirmation'}")
            for i, c in enumerate(ctx.pending.get("candidates") or [], 1):
                facts.append(f"  option {i}: {c.get('summary') or c.get('subject')} ({c.get('start') or c.get('date') or ''})")
        if ctx.memory.get("last_contact_email"):
            facts.append(f"- email address in focus: {ctx.memory['last_contact_email']}")
        for i, e in enumerate((ctx.memory.get("emails") or [])[:5], 1):
            facts.append(f"- email {i}: \"{e.get('subject')}\" from {e.get('sender')}")
        for i, e in enumerate((ctx.memory.get("events") or [])[:5], 1):
            facts.append(f"- event {i}: \"{e.get('summary')}\" at {e.get('start')}")
        if facts:
            lines += ["[Known facts from this conversation]", *facts]
        return "\n".join(lines)

    # ---------- enhancement ----------
    def _continues(self, ir: IntentResult, message: str, pending: dict | None) -> bool:
        if not pending:
            return False
        if ir.intent in ("unknown", pending.get("intent")):
            return True
        # A short answer that picks one of the options we just listed continues the request even if the
        # model guessed a different intent for the fragment ("The project review.").
        return bool(pending.get("candidates")) and len(message.split()) <= 8 and \
            select_candidate(message, pending["candidates"]) is not None

    def enhance(self, ir: IntentResult, message: str, ctx: ConversationContext) -> EnhancedRequest:
        pending, memory = ctx.pending, ctx.memory
        if pending and ir.intent == "unknown" and _ABANDON.search(message):
            return EnhancedRequest("unknown", "unknown", {}, abandon=True, notes=["user abandoned the pending request"])

        continued = self._continues(ir, message, pending)
        notes: list[str] = []
        new = ir.entities.model_dump(exclude_none=True)
        new = {k: v for k, v in new.items() if v not in ([], "")}
        recent_assistant_message = next((h["content"] for h in reversed(ctx.history) if h["role"] == "assistant"), "")
        email_followup = (
            not pending
            and memory.get("last_email_id")
            and "email from " in recent_assistant_message.casefold()
            and _EMAIL_DETAIL_FOLLOWUP.search(message)
        )
        if email_followup:
            intent = "read_email"
            entities = {"message_id": memory["last_email_id"]}
            notes.append("email follow-up resolved to the last email discussed")
        elif continued:
            assert pending is not None
            intent = pending["intent"]
            entities = {**(pending.get("entities") or {}), **new}
            notes.append(f"continued pending {intent}")
            awaiting = set(pending.get("awaiting") or [])
            if "time" not in new and awaiting & {"time", "new_date_or_time"}:
                if t := extract_time(message):
                    entities["time"] = t
                    notes.append("time taken from the follow-up")
            if "date" not in new and awaiting & {"date", "new_date_or_time"}:
                if resolve_date(message, ctx.reference_date):
                    entities["date"] = message.strip()
        else:
            intent = ir.intent
            entities = new
        service = INTENT_TO_SERVICE.get(intent, "unknown")

        # pending candidate selection ("The project review.", "the second one")
        if continued and pending and pending.get("candidates") and not entities.get("event_id") \
                and not entities.get("message_id"):
            chosen = select_candidate(message, pending["candidates"],
                                      "subject" if pending.get("candidate_type") == "email" else "summary")
            if chosen:
                key = "message_id" if pending.get("candidate_type") == "email" else "event_id"
                entities[key] = chosen["id"]
                notes.append(f"selected {key} from the options shown")

        self._resolve_references(intent, message, entities, memory, notes)

        req = EnhancedRequest(service, intent, entities, continued_pending=continued, notes=notes)
        if not continued and not email_followup and ir.requires_clarification and ir.clarification_question \
                and ir.intent != "unknown":
            req.llm_question = ir.clarification_question
        if intent in CALENDAR_INTENTS and entities.get("date"):
            text = str(entities["date"])
            resolved = resolve_date(text, ctx.reference_date)
            req.date_text = text
            if resolved:
                entities["date"] = resolved.isoformat()
            else:
                req.date_unresolved = True
                entities.pop("date")
        return req

    def _resolve_references(self, intent: str, message: str, entities: dict[str, Any], memory: dict[str, Any],
                            notes: list[str]) -> None:
        if intent in ("send_email", "create_draft") and not entities.get("recipient_email"):
            person = (entities.get("person") or "").strip()
            pronoun = bool(_PRONOUN_PERSON.search(message)) or bool(_PRONOUN_PERSON.fullmatch(person))
            contact = memory.get("last_contact_email")
            if contact and is_valid_email(contact) and (pronoun or not person):
                entities["recipient_email"] = contact
                notes.append("recipient resolved from earlier in the conversation")
                if _PRONOUN_PERSON.fullmatch(person):
                    entities.pop("person", None)

        if intent in ("read_email", "reply_email", "summarize_emails") and not entities.get("message_id"):
            message = _TIME_SPAN.sub(" ", message)  # "emails from last week" is a time range, not a reference
            if _EMAIL_REF.search(message):
                emails = memory.get("emails") or []
                person = entities.get("person") or entities.get("recipient_email")
                if person:  # "the latest one from Rahul" must not pick an email from someone else
                    emails = [e for e in emails if _tokens(person) & _tokens(e.get("sender"))]
                chosen = _pick_email(message, emails)
                if chosen:
                    entities["message_id"] = chosen["id"]
                    entities.pop("query", None)
                    notes.append("email reference resolved from the emails shown earlier")
                elif memory.get("last_email_id") and re.search(r"\b(it|that|this)\b", message, re.I):
                    entities["message_id"] = memory["last_email_id"]
                    entities.pop("query", None)
                    notes.append("email reference resolved to the last email discussed")

        if intent in ("reschedule_event", "cancel_event") and not entities.get("event_id"):
            if memory.get("last_event_id") and _EVENT_REF.search(message) and not (entities.get("person") or entities.get("title")):
                entities["event_id"] = memory["last_event_id"]
                notes.append("event reference resolved to the last event discussed")

    # ---------- persistence ----------
    @staticmethod
    def save(db: Session, ctx: ConversationContext, *, memory_updates: dict[str, Any], pending: dict | None) -> None:
        sc = db.scalar(select(SessionContext).where(SessionContext.conversation_id == ctx.conversation_id))
        if sc is None:
            sc = SessionContext(conversation_id=ctx.conversation_id, entities={})
            db.add(sc)
        memory = {**(sc.entities or {}), **memory_updates}
        for key in ("emails", "events"):
            if key in memory:
                memory[key] = list(memory[key])[:MEMORY_LIST_LIMIT]
        sc.entities = memory
        sc.pending_intent = pending
        sc.reference_date = ctx.reference_date
        sc.timezone = ctx.timezone


def entities_model(entities: dict[str, Any]) -> Entities:
    return Entities.model_validate(entities)
