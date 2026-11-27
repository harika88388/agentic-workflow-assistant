import base64
import email

import pytest

from app.integrations.gmail import mime


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")  # Gmail omits padding


def _b64_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode_raw(raw: str):
    return email.message_from_bytes(base64.urlsafe_b64decode(raw))


def test_build_message_roundtrip():
    raw = mime.build_raw_message(to=["Prof <professor@example.com>"], subject="Lecture", body="I'll miss today's lecture")
    m = _decode_raw(raw)
    assert m["To"] == "professor@example.com" and m["Subject"] == "Lecture"
    assert "miss today's lecture" in m.get_payload(decode=True).decode()


def test_header_injection_is_rejected():
    for kwargs in [dict(to=["a@example.com\r\nBcc: evil@x.com"], subject="s"),
                   dict(to=["a@example.com"], subject="hi\r\nBcc: evil@x.com"),
                   dict(to=["a@example.com, b@example.com"], subject="s"),
                   dict(to=["not-an-email"], subject="s"),
                   dict(to=[], subject="s")]:
        with pytest.raises(ValueError):
            mime.build_raw_message(body="hello", **kwargs)


def test_empty_body_rejected_and_blank_subject_defaulted():
    with pytest.raises(ValueError):
        mime.build_raw_message(to=["a@example.com"], subject="x", body="  ")
    assert _decode_raw(mime.build_raw_message(to=["a@example.com"], subject="", body="hi"))["Subject"] == "(no subject)"


def test_unicode_body_survives():
    raw = mime.build_raw_message(to=["a@example.com"], subject="Grüße", body="नमस्ते 👋")
    assert "नमस्ते 👋" in _decode_raw(raw).get_payload(decode=True).decode("utf-8")


def test_reply_threading_headers():
    target = mime.EmailMessageData(id="1", thread_id="t", subject="Plan", message_id_header="<b@x>", references="<a@x>")
    assert mime.reply_headers(target) == ("<b@x>", "<a@x> <b@x>")
    assert mime.reply_subject("Plan") == "Re: Plan" and mime.reply_subject("Re: Plan") == "Re: Plan"
    raw = mime.build_raw_message(to=["a@example.com"], subject="Re: Plan", body="ok", in_reply_to="<b@x>", references="<a@x> <b@x>")
    m = _decode_raw(raw)
    assert m["In-Reply-To"] == "<b@x>" and m["References"] == "<a@x> <b@x>"


def test_parse_message_prefers_plain_and_handles_missing_padding():
    raw = {"id": "m1", "threadId": "t1", "snippet": "Hi &amp; bye", "labelIds": ["INBOX"], "internalDate": "1700000000000",
           "payload": {"headers": [{"name": "From", "value": "A <a@x.com>"}, {"name": "subject", "value": "Hello"},
                                   {"name": "Message-Id", "value": "<m@x>"}],
                       "mimeType": "multipart/alternative",
                       "parts": [{"mimeType": "text/plain", "body": {"data": _b64("plain body!")}},
                                 {"mimeType": "text/html", "body": {"data": _b64("<b>html body</b>")}}]}}
    m = mime.parse_message(raw)
    assert m.body_text == "plain body!" and m.subject == "Hello" and m.message_id_header == "<m@x>"
    assert m.snippet == "Hi & bye" and m.internal_date.startswith("2023-11-14") and m.untrusted is True


def test_html_fallback_strips_scripts_and_decodes_entities():
    html = "<html><style>p{}</style><script>alert(1)</script><p>Hello&nbsp;<b>World</b></p><p>2 &lt; 3</p></html>"
    t = mime.parse_message({"id": "1", "payload": {"mimeType": "text/html", "body": {"data": _b64(html)}}}).body_text
    assert "alert" not in t and "p{}" not in t and "Hello" in t and "World" in t and "2 < 3" in t


def test_nested_multipart_prefers_plain_text_and_decodes_declared_charset():
    raw = {"id": "1", "payload": {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/html", "body": {"data": _b64("<p>html version</p>")}},
            {"mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": "text/plain; charset=iso-8859-1"}],
             "body": {"data": _b64_bytes("caf\xe9".encode("latin-1"))}},
        ]},
    ]}}
    assert mime.parse_message(raw).body_text == "café"


def test_empty_or_malformed_message_body_is_handled_without_using_snippet():
    empty = mime.parse_message({"id": "1", "snippet": "subject preview only", "payload": {}})
    malformed = mime.parse_message({"id": "2", "snippet": "preview", "payload": {
        "mimeType": "text/plain", "body": {"data": "%%%not-base64%%%"},
    }})
    assert empty.body_text == "" and malformed.body_text == ""


def test_attachments_listed_not_inlined_and_long_body_truncated():
    raw = {"id": "1", "payload": {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "text/plain", "body": {"data": _b64("x" * 30000)}},
        {"mimeType": "application/pdf", "filename": "cv.pdf", "body": {"attachmentId": "A1", "size": 10}}]}}
    m = mime.parse_message(raw)
    assert m.attachments == [{"filename": "cv.pdf", "mime_type": "application/pdf", "size": 10}]
    assert m.body_text.endswith("[truncated]") and len(m.body_text) < 20100
    assert mime.parse_message(raw, include_body=False).body_text == ""
