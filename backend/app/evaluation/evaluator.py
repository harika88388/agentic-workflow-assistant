"""Deterministic offline evaluation harness for intent classification.

This measures the repository's routing contract without making external LLM calls.
A real provider can supply a callable that accepts a message and returns an IntentResult.
"""
from collections.abc import Callable
from dataclasses import dataclass, asdict

from app.schemas.intent import IntentResult


@dataclass(frozen=True)
class EvalCase:
    message: str
    service: str
    intent: str


def default_dataset() -> list[EvalCase]:
    """Return at least 200 labeled examples generated from stable templates."""
    cases: list[EvalCase] = []
    gmail_actions = (
        ("send_email", "Send an email to person{i}@example.com saying project update {i}"),
        ("create_draft", "Create a draft for person{i}@example.com about meeting {i}"),
        ("search_emails", "Find invoice email number {i}"),
        ("reply_email", "Reply to message m{i} saying thanks for update {i}"),
        ("summarize_emails", "Summarize my latest emails about topic {i}"),
    )
    calendar_actions = (
        ("create_event", "Schedule project sync {i} at 3 PM tomorrow"),
        ("reschedule_event", "Move meeting {i} to Friday at 4 PM"),
        ("cancel_event", "Cancel meeting {i}"),
        ("get_events", "Show my meetings for day {i}"),
        ("check_availability", "Check availability tomorrow at 2 PM for slot {i}"),
    )
    for i in range(1, 21):
        for intent, template in gmail_actions + calendar_actions:
            service = "gmail" if intent in {x[0] for x in gmail_actions} else "calendar"
            cases.append(EvalCase(template.format(i=i), service, intent))
    return cases


def evaluate(predict: Callable[[str], IntentResult], cases: list[EvalCase] | None = None) -> dict:
    cases = cases or default_dataset()
    passed = 0
    failures: list[dict] = []
    for case in cases:
        try:
            result = predict(case.message)
            ok = result.service == case.service and result.intent == case.intent
        except Exception as exc:  # noqa: BLE001 - evaluation must continue and report failures
            ok = False
            result = None
            failures.append({"message": case.message, "expected": [case.service, case.intent],
                             "error": f"{type(exc).__name__}: {exc}"})
            continue
        if ok:
            passed += 1
        else:
            failures.append({"message": case.message, "expected": [case.service, case.intent],
                             "actual": [result.service, result.intent]})
    total = len(cases)
    return {"total": total, "passed": passed, "failed": total - passed,
            "accuracy": round(passed / total, 4) if total else 0.0,
            "failures": failures[:25]}
