"""Gmail REST API v1 client (users/me)."""
import logging
from collections.abc import Callable
from urllib.parse import quote

from app.integrations.errors import GoogleNotFoundError
from app.integrations.gmail.mime import (
    EmailMessageData, EmailThreadData, describe_payload, parse_message, referenced_text_parts,
)
from app.integrations.http import GoogleHTTP

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_SEARCH = 25
log = logging.getLogger(__name__)


def _id(value: str) -> str:
    if not value or not value.strip():
        raise ValueError("id must not be empty")
    return quote(value, safe="")  # prevents path traversal via crafted ids


class GmailClient:
    def __init__(self, token_provider: Callable[[], str], **http_kwargs):
        self.http = GoogleHTTP(BASE, token_provider, **http_kwargs)

    def search(self, query: str, max_results: int = 10) -> list[EmailMessageData]:
        max_results = max(1, min(int(max_results), MAX_SEARCH))
        log.info("gmail search initiated query=%r max_results=%d", query[:200], max_results)
        data = self.http.request("GET", "/messages", params={"q": query, "maxResults": max_results}) or {}
        out: list[EmailMessageData] = []
        for ref in data.get("messages", []):
            try:
                raw = self.http.request(
                    "GET", f"/messages/{_id(ref['id'])}",
                    params={"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]},
                )
            except GoogleNotFoundError:
                continue  # deleted between list and fetch
            out.append(parse_message(raw, include_body=False))
        log.info("gmail search completed result_count=%d estimated_total=%s", len(out),
                 data.get("resultSizeEstimate", "-"))
        return out

    def _inline_referenced_parts(self, raw: dict) -> dict:
        referenced = referenced_text_parts(raw.get("payload") or {})
        for part in referenced:
            att = self.http.request(
                "GET", f"/messages/{_id(str(raw.get('id') or ''))}/attachments/{_id(part['body']['attachmentId'])}") or {}
            part["body"] = {**part["body"], "data": att.get("data") or ""}
        if referenced:
            log.info("gmail large body parts fetched message_id=%s parts=%d", raw.get("id"), len(referenced))
        return raw

    def _parse_full(self, raw: dict) -> EmailMessageData:
        message = parse_message(self._inline_referenced_parts(raw))
        stats = describe_payload(raw.get("payload") or {}, message.body_text)
        log.info("gmail full message parsed message_id=%s format=full body_source=%s body_chars=%d mime_parts=%d "
                 "text_plain_parts=%d text_html_parts=%d attachments=%d truncated=%s", message.id,
                 stats["body_source"], stats["body_chars"], stats["mime_parts"], stats["text_plain_parts"],
                 stats["text_html_parts"], stats["attachments"], stats["truncated"])
        if not message.body_text:
            log.warning("gmail message has no readable body message_id=%s mime_type=%s", message.id,
                        stats["mime_type"])
        return message

    def get_message(self, message_id: str) -> EmailMessageData:
        raw = self.http.request("GET", f"/messages/{_id(message_id)}", params={"format": "full"})
        return self._parse_full(raw)

    def get_thread(self, thread_id: str) -> EmailThreadData:
        raw = self.http.request("GET", f"/threads/{_id(thread_id)}", params={"format": "full"})
        return EmailThreadData(id=raw["id"], messages=[self._parse_full(m) for m in raw.get("messages", [])])

    def send_raw(self, raw_message: str, thread_id: str | None = None) -> dict:
        body: dict = {"raw": raw_message}
        if thread_id:
            body["threadId"] = thread_id
        return self.http.request("POST", "/messages/send", json=body)  # NOT retried on 5xx/timeout

    def create_draft_raw(self, raw_message: str, thread_id: str | None = None) -> dict:
        message: dict = {"raw": raw_message}
        if thread_id:
            message["threadId"] = thread_id
        return self.http.request("POST", "/drafts", json={"message": message})

    def get_labels(self, message_id: str) -> list[str]:
        raw = self.http.request("GET", f"/messages/{_id(message_id)}", params={"format": "minimal"})
        return list(raw.get("labelIds") or [])

    def draft_exists(self, draft_id: str) -> bool:
        try:
            self.http.request("GET", f"/drafts/{_id(draft_id)}", params={"format": "minimal"})
            return True
        except GoogleNotFoundError:
            return False
