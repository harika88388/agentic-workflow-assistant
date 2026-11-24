"""Gmail REST API v1 client (users/me)."""
from collections.abc import Callable
from urllib.parse import quote

from app.integrations.errors import GoogleNotFoundError
from app.integrations.gmail.mime import EmailMessageData, EmailThreadData, parse_message
from app.integrations.http import GoogleHTTP

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_SEARCH = 25


def _id(value: str) -> str:
    if not value or not value.strip():
        raise ValueError("id must not be empty")
    return quote(value, safe="")  # prevents path traversal via crafted ids


class GmailClient:
    def __init__(self, token_provider: Callable[[], str], **http_kwargs):
        self.http = GoogleHTTP(BASE, token_provider, **http_kwargs)

    def search(self, query: str, max_results: int = 10) -> list[EmailMessageData]:
        max_results = max(1, min(int(max_results), MAX_SEARCH))
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
        return out

    def get_message(self, message_id: str) -> EmailMessageData:
        return parse_message(self.http.request("GET", f"/messages/{_id(message_id)}", params={"format": "full"}))

    def get_thread(self, thread_id: str) -> EmailThreadData:
        raw = self.http.request("GET", f"/threads/{_id(thread_id)}", params={"format": "full"})
        return EmailThreadData(id=raw["id"], messages=[parse_message(m) for m in raw.get("messages", [])])

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
