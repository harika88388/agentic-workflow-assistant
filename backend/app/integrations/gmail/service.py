"""Gmail operations with post-action verification. Each write confirms against Gmail before reporting success."""
from dataclasses import dataclass
from email.utils import parseaddr

from app.integrations.errors import GoogleAPIError
from app.integrations.gmail.client import GmailClient
from app.integrations.gmail.mime import (
    EmailMessageData,
    EmailThreadData,
    build_raw_message,
    reply_headers,
    reply_subject,
    validate_address,
)


class GmailOperationError(Exception):
    pass


@dataclass
class SendResult:
    id: str
    thread_id: str
    to: list[str]
    subject: str
    verified: bool
    note: str = ""


@dataclass
class DraftResult:
    id: str
    message_id: str
    thread_id: str
    to: list[str]
    subject: str
    verified: bool


class GmailService:
    def __init__(self, client: GmailClient):
        self.client = client

    # --- read ---
    def search_emails(self, query: str, max_results: int = 10) -> list[EmailMessageData]:
        if not query or not query.strip():
            raise ValueError("search query must not be empty")
        return self.client.search(query.strip(), max_results)

    def read_email(self, message_id: str) -> EmailMessageData:
        return self.client.get_message(message_id)

    def get_thread(self, thread_id: str) -> EmailThreadData:
        return self.client.get_thread(thread_id)

    # --- write ---
    def send_email(
        self, to: list[str], subject: str, body: str, cc: list[str] | None = None, bcc: list[str] | None = None
    ) -> SendResult:
        raw = build_raw_message(to=to, subject=subject, body=body, cc=cc, bcc=bcc)
        sent = self.client.send_raw(raw)
        return self._verify_sent(sent, [validate_address(a) for a in to], subject or "(no subject)", None)

    def reply_email(self, body: str, *, message_id: str | None = None, thread_id: str | None = None) -> SendResult:
        if not (message_id or thread_id):
            raise ValueError("message_id or thread_id is required to reply")
        if message_id:
            thread_id = self.client.get_message(message_id).thread_id
        thread = self.client.get_thread(thread_id)  # type: ignore[arg-type]
        if not thread.messages:
            raise GmailOperationError("thread has no messages")
        target, recipient = self._reply_target(thread)
        subject = reply_subject(target.subject)
        in_reply_to, references = reply_headers(target)
        raw = build_raw_message(
            to=[recipient], subject=subject, body=body, in_reply_to=in_reply_to, references=references
        )
        sent = self.client.send_raw(raw, thread_id=thread.id)
        return self._verify_sent(sent, [recipient], subject, thread.id)

    def create_draft(self, to: list[str], subject: str, body: str, thread_id: str | None = None) -> DraftResult:
        raw = build_raw_message(to=to, subject=subject, body=body)
        d = self.client.create_draft_raw(raw, thread_id)
        draft_id = d.get("id")
        if not draft_id:
            raise GmailOperationError("Gmail did not return a draft id")
        try:
            verified = self.client.draft_exists(draft_id)
        except GoogleAPIError:
            verified = False
        msg = d.get("message") or {}
        return DraftResult(draft_id, msg.get("id", ""), msg.get("threadId", ""),
                           [validate_address(a) for a in to], subject or "(no subject)", verified)

    # --- helpers ---
    @staticmethod
    def _reply_target(thread: EmailThreadData) -> tuple[EmailMessageData, str]:
        """Reply to the latest message not written by the user; if all are, to the last one's first recipient.
        Uses From (not Reply-To) so an attacker-controlled Reply-To cannot silently redirect a reply."""
        incoming = [m for m in thread.messages if "SENT" not in m.labels and "DRAFT" not in m.labels]
        if incoming:
            target = incoming[-1]
            addr = parseaddr(target.sender)[1]
        else:
            target = thread.messages[-1]
            addr = parseaddr(target.to.split(",")[0])[1]
        try:
            return target, validate_address(addr)
        except ValueError as exc:
            raise GmailOperationError("could not determine a valid reply address") from exc

    def _verify_sent(self, sent: dict, to: list[str], subject: str, expect_thread: str | None) -> SendResult:
        msg_id = sent.get("id")
        if not msg_id:
            raise GmailOperationError("Gmail did not confirm the send (no message id returned)")
        thread_id = sent.get("threadId", "")
        try:
            verified = "SENT" in self.client.get_labels(msg_id)
        except GoogleAPIError:
            return SendResult(msg_id, thread_id, to, subject, False, "Gmail accepted the message but it could not be re-checked.")
        if expect_thread and thread_id != expect_thread:
            return SendResult(msg_id, thread_id, to, subject, False, "Message was sent but not placed in the original thread.")
        return SendResult(msg_id, thread_id, to, subject, verified, "" if verified else "Sent label not found on message.")
