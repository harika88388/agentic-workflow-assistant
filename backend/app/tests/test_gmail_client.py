from app.integrations.gmail.client import GmailClient


def test_read_requests_full_gmail_message_and_parses_its_body(monkeypatch):
    client = GmailClient(lambda: "not-used")
    requests = []
    raw = {
        "id": "message-1",
        "threadId": "thread-1",
        "snippet": "The subject only",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "aacorner <sender@example.com>"},
                {"name": "To", "value": "recipient@example.com"},
                {"name": "Subject", "value": "You are gay"},
                {"name": "Date", "value": "Fri, 27 Nov 2026 10:00:00 +0000"},
            ],
            "body": {"data": "VGhpcyBpcyB0byBpbmZvcm0geW91IHRoYXQgYSBtZWV0aW5nIGhhcyBiZWVuIHNjaGVkdWxlZCB0b25pZ2h0Lg"},
        },
    }

    def request(method, path, **kwargs):
        requests.append((method, path, kwargs))
        return raw

    monkeypatch.setattr(client.http, "request", request)
    message = client.get_message("message-1")

    assert requests == [("GET", "/messages/message-1", {"params": {"format": "full"}})]
    assert message.id == "message-1" and message.thread_id == "thread-1"
    assert message.sender == "aacorner <sender@example.com>" and message.to == "recipient@example.com"
    assert message.subject == "You are gay" and "meeting has been scheduled tonight" in message.body_text
    assert message.body_text != message.snippet
