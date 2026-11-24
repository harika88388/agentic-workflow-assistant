from app.database.models import Conversation
from app.services import workflow_service


def test_workflow_requires_approval_and_rejects(make_user, db_session):
    user, _ = make_user()
    conversation = Conversation(user_id=user.id, title="Inbox")
    db_session.add(conversation)
    db_session.commit()
    execution = workflow_service.propose(
        db_session, user, conversation_id=conversation.id, service="gmail", intent="send_email",
        request={"to": ["a@example.com"], "body": "Hello"}, idempotency_key="one",
    )
    assert execution.status == "awaiting_approval"
    rejected = workflow_service.reject(db_session, user, execution.id)
    assert rejected.status == "rejected"


def test_duplicate_idempotency_returns_existing(make_user, db_session):
    user, _ = make_user()
    conversation = Conversation(user_id=user.id, title="Inbox")
    db_session.add(conversation)
    db_session.commit()
    kwargs = dict(db=db_session, user=user, conversation_id=conversation.id, service="gmail",
                  intent="create_draft", request={"to": ["a@example.com"], "body": "Draft"}, idempotency_key="same")
    first = workflow_service.propose(**kwargs)
    second = workflow_service.propose(**kwargs)
    assert first.id == second.id


def test_approved_gmail_workflow_persists_result(make_user, db_session):
    user, _ = make_user()
    conversation = Conversation(user_id=user.id, title="Inbox")
    db_session.add(conversation)
    db_session.commit()
    execution = workflow_service.propose(
        db_session, user, conversation_id=conversation.id, service="gmail", intent="send_email",
        request={"to": ["a@example.com"], "subject": "Hi", "body": "Hello"}, idempotency_key="send-one",
    )

    class FakeGmail:
        def send_email(self, to, subject, body, cc=None, bcc=None):
            return {"id": "sent-1", "verified": True}

    completed = workflow_service.execute(db_session, user, execution.id, gmail=FakeGmail())
    assert completed.status == "succeeded"
    assert completed.result == {"value": {"id": "sent-1", "verified": True}}
