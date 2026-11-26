"""Service Selector Agent: decides whether Gmail or Google Calendar should handle a request.

It runs before intent recognition (to give the Intent Recognition Agent a service hint) and again after it
(to fix the final route). It never classifies the operation itself: that stays with the intent agent.
"""
import re
from dataclasses import asdict, dataclass
from typing import Literal

from app.schemas.intent import IntentResult

Service = Literal["gmail", "calendar", "unknown"]

_EMAIL_ADDRESS = re.compile(r"[\w.%+'-]+@[\w-]+(?:\.[\w-]+)+")
_SIGNALS: dict[str, list[tuple[re.Pattern[str], float]]] = {
    "gmail": [
        (re.compile(r"\b(e-?mails?|gmail|inbox|mails?)\b"), 3.0),
        (re.compile(r"\b(repl(y|ies)|respond|draft|forward|unread|attachments?|subject|sender)\b"), 2.0),
        (re.compile(r"\b(send|write|tell|message|notify)\b"), 1.0),
        (re.compile(r"\bsummar(ise|ize|y)\b"), 0.5),
    ],
    "calendar": [
        (re.compile(r"\b(calendar|meetings?|events?|appointments?|schedul\w*|reschedul\w*)\b"), 3.0),
        (re.compile(r"\b(availab\w*|free|busy|invite|book|postpone|call|standup|sync)\b"), 2.0),
        (re.compile(r"\b(move|cancel|agenda|slot)\b"), 1.0),
    ],
}
MIN_SCORE = 1.0
MARGIN = 1.0


@dataclass(frozen=True)
class ServiceSelection:
    service: Service
    confidence: float
    source: Literal["signals", "pending_context", "intent", "none"]
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


class ServiceSelectorAgent:
    def score(self, message: str) -> dict[str, float]:
        text = (message or "").casefold()
        scores = {svc: sum(w for pat, w in pats if pat.search(text)) for svc, pats in _SIGNALS.items()}
        if _EMAIL_ADDRESS.search(text) and scores["calendar"] < 3.0:
            scores["gmail"] += 2.0  # an address with no meeting vocabulary is almost always an email
        return scores

    def select(self, message: str, *, pending_service: str | None = None) -> ServiceSelection:
        """Pre-intent selection from the message's own signals, falling back to the conversation's pending request."""
        scores = self.score(message)
        best, other = sorted(scores, key=scores.get, reverse=True)  # type: ignore[arg-type]
        if scores[best] >= MIN_SCORE and scores[best] - scores[other] >= MARGIN:
            conf = min(0.95, 0.5 + 0.1 * (scores[best] - scores[other]))
            return ServiceSelection(best, round(conf, 2), "signals", f"{best} vocabulary in the message")  # type: ignore[arg-type]
        if pending_service in ("gmail", "calendar"):
            return ServiceSelection(pending_service, 0.6, "pending_context",  # type: ignore[arg-type]
                                    "no clear signal; continuing the conversation's pending request")
        return ServiceSelection("unknown", 0.0, "none", "no service signal in the message")

    def reconcile(self, selection: ServiceSelection, intent: IntentResult, *,
                  continued_service: str | None = None) -> ServiceSelection:
        """Final route. A recognised intent determines its service; otherwise a continued request or the
        pre-intent signals decide."""
        if intent.intent != "unknown":
            agree = selection.service in ("unknown", intent.service)
            reason = (f"intent '{intent.intent}' belongs to {intent.service}" if agree else
                      f"intent '{intent.intent}' belongs to {intent.service}; overrides {selection.service} signals")
            return ServiceSelection(intent.service, intent.confidence or 0.8, "intent", reason)
        if continued_service in ("gmail", "calendar"):
            return ServiceSelection(continued_service, 0.7, "pending_context",  # type: ignore[arg-type]
                                    "follow-up completes the pending request")
        return selection
