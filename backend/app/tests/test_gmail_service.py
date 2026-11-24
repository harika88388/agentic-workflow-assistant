import base64
import email

import pytest

from app.integrations.errors import GoogleAPIError
from app.integrations.gmail.mime import EmailMessageData, EmailThreadData
from app.integrations.gmail.service import GmailOperationError, GmailService


class FakeGmail:
    def __init__(self, thread=None, labels=("SENT",), send_result=None, labels_error=False):
        self.thread, self.labels, self.labels_error = thread, list(labels), labels_error
        self.send_result = send_result if send_result is not None else {"id": "sent1", "threadId": "t1"}
        self.sent = []
        self.drafts = []

    def get_message(self, mid):
        return EmailMessageData(id=mid, thread_id="t1")

    def get_thread(self, tid):
        return self.thread

    def send_raw(self, raw, thread_id=None):
        self.sent.append((raw, thread_id))
        return self.send_result

    def get_labels(self, mid):
        if self.labels_error:
            raise GoogleAPIError("boom")
        return self.labels

    def create_draft_raw(self, raw, thread_id=None):
        self.drafts.append(raw)
        return {"id": "d1", "message": {"id": "m9", "threadId": "t9"}}

    def draft_exists(self, did):
        return True


def _msg(id, sender, subject="Plan", labels=(), to="me@example.com"):
    return EmailMessageData(id=id, thread_id="t1", sender=sender, to=to, subject=subject, labels=list(labels),
                            message_id_header=f"<{id}@x>", references="<root@x>")


def _hdr(raw):
    return email.message_from_bytes(base64.urlsafe_b64decode(raw))


def test_send_verified_when_sent_label_present():
    g = FakeGmail()
    r = GmailService(g).send_email(["prof@example.com"], "Hi", "Body")
    assert r.verified and r.id == "sent1" and r.to == ["prof@example.com"] and len(g.sent) == 1


def test_send_not_verified_when_label_missing_or_recheck_fails():
    assert not GmailService(FakeGmail(labels=["INBOX"])).send_email(["a@example.com"], "s", "b").verified
    r = GmailService(FakeGmail(labels_error=True)).send_email(["a@example.com"], "s", "b")
    assert not r.verified and "could not be re-checked" in r.note


def test_send_without_message_id_is_an_error_not_success():
    with pytest.raises(GmailOperationError):
        GmailService(FakeGmail(send_result={})).send_email(["a@example.com"], "s", "b")


def test_invalid_recipient_never_reaches_gmail():
    g = FakeGmail()
    with pytest.raises(ValueError):
        GmailService(g).send_email(["Rahul"], "s", "b")
    assert g.sent == []


def test_reply_targets_latest_incoming_message_in_same_thread():
    thread = EmailThreadData("t1", [_msg("1", "Prof <prof@example.com>"), _msg("2", "me@example.com", labels=["SENT"]),
                                    _msg("3", "TA <ta@example.com>", subject="Re: Plan")])
    g = FakeGmail(thread=thread)
    r = GmailService(g).reply_email("Thanks!", message_id="3")
    raw, thread_id = g.sent[0]
    m = _hdr(raw)
    assert thread_id == "t1" and m["To"] == "ta@example.com" and m["Subject"] == "Re: Plan"
    assert m["In-Reply-To"] == "<3@x>" and m["References"] == "<root@x> <3@x>"
    assert r.verified


def test_reply_to_own_thread_goes_to_original_recipient():
    thread = EmailThreadData("t1", [_msg("1", "me@example.com", labels=["SENT"], to="Bob <bob@example.com>, c@example.com")])
    g = FakeGmail(thread=thread)
    GmailService(g).reply_email("ping", thread_id="t1")
    assert _hdr(g.sent[0][0])["To"] == "bob@example.com"


def test_reply_flags_unthreaded_result_and_requires_ids():
    thread = EmailThreadData("t1", [_msg("1", "p@example.com")])
    g = FakeGmail(thread=thread, send_result={"id": "s", "threadId": "OTHER"})
    r = GmailService(g).reply_email("x", thread_id="t1")
    assert not r.verified and "thread" in r.note
    with pytest.raises(ValueError):
        GmailService(g).reply_email("x")


def test_reply_with_unusable_sender_fails_safely():
    thread = EmailThreadData("t1", [_msg("1", "undisclosed-recipients:;")])
    g = FakeGmail(thread=thread)
    with pytest.raises(GmailOperationError):
        GmailService(g).reply_email("x", thread_id="t1")
    assert g.sent == []


def test_create_draft_verified():
    r = GmailService(FakeGmail()).create_draft(["a@example.com"], "S", "B")
    assert r.id == "d1" and r.verified and r.thread_id == "t9"


def test_search_requires_query():
    with pytest.raises(ValueError):
        GmailService(FakeGmail()).search_emails("  ")
