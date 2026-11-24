import pytest

from app.services.llm.jsonutil import JSONExtractionError, extract_json, strip_reasoning


def test_plain_and_fenced_and_surrounded():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Here you go:\n```json\n{"a": 1}\n```\nDone') == {"a": 1}
    assert extract_json('Sure! {"a": {"b": "}"}} hope that helps') == {"a": {"b": "}"}}


def test_r1_think_blocks_removed_even_if_they_contain_json_or_braces():
    raw = '<think>maybe {"intent":"wrong"} hmm</think>\n{"intent": "send_email"}'
    assert extract_json(raw) == {"intent": "send_email"}
    assert extract_json('reasoning without opener</think>{"x": 2}') == {"x": 2}


def test_unterminated_think_is_not_mined_for_json():
    with pytest.raises(JSONExtractionError):
        extract_json('<think>I will answer {"intent": "send_email"} but never finish')
    assert strip_reasoning("<think>a</think><think>b</think>ok") == "ok"


def test_garbage_and_empty_raise():
    for bad in ["no json here", "", "<think>only thoughts</think>", "{not json}"]:
        with pytest.raises(JSONExtractionError):
            extract_json(bad)


def test_picks_first_valid_object_after_invalid_braces():
    assert extract_json('use {curly} braces then {"ok": true}') == {"ok": True}
