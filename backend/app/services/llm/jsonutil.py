"""Stdlib-only helpers for turning raw LLM text (possibly with <think> blocks / code fences) into JSON."""
import json
import re
from collections.abc import Iterator
from typing import Any


class JSONExtractionError(ValueError):
    pass


def strip_reasoning(text: str) -> str:
    """Remove DeepSeek-R1 style <think>…</think> reasoning."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    if re.search(r"</think>", text, re.I):  # opening tag missing
        text = re.split(r"</think>", text, flags=re.I)[-1]
    elif re.search(r"<think>", text, re.I):  # reasoning never closed: nothing usable after it
        text = re.split(r"<think>", text, flags=re.I)[0]
    return text.strip()


def _balanced_objects(text: str) -> Iterator[str]:
    for start in (i for i, c in enumerate(text) if c == "{"):
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : i + 1]
                    break


def extract_json(text: str) -> Any:
    t = strip_reasoning(text)
    if not t:
        raise JSONExtractionError("empty response after removing reasoning")
    try:
        return json.loads(t)
    except ValueError:
        pass
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", t, re.S | re.I):
        try:
            return json.loads(m.group(1).strip())
        except ValueError:
            continue
    for cand in _balanced_objects(t):
        try:
            return json.loads(cand)
        except ValueError:
            continue
    raise JSONExtractionError("no valid JSON object found in model output")
