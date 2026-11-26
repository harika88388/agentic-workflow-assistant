from app.evaluation.evaluator import default_dataset, evaluate
from app.schemas.intent import IntentResult


def test_evaluation_dataset_contains_at_least_200_queries():
    assert len(default_dataset()) >= 200


def test_evaluation_reports_accuracy_and_failures():
    def predict(message: str) -> IntentResult:
        if "email" in message.lower() or "draft" in message.lower() or "invoice" in message.lower():
            intent = "create_draft" if "draft" in message.lower() else ("search_emails" if "invoice" in message.lower() else "send_email")
            return IntentResult(service="gmail", intent=intent)
        if "cancel" in message.lower():
            return IntentResult(service="calendar", intent="cancel_event")
        return IntentResult(service="calendar", intent="create_event")

    report = evaluate(predict)
    assert report["total"] == 200
    assert 0.0 <= report["accuracy"] <= 1.0
    assert report["failed"] == report["total"] - report["passed"]
