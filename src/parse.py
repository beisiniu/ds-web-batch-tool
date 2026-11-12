from __future__ import annotations

import json
import re
import unicodedata
from typing import Any


class ParseError(ValueError):
    pass


def _strip_fences(text: str) -> list[str]:
    blocks = re.findall(r"```(?:json|JSON)?\s*(.*?)```", text, re.S)
    blocks = [b.strip() for b in blocks if b.strip()]
    if blocks:
        blocks.sort(key=len, reverse=True)
        return blocks + [text]
    return [text]


def _trim_to_last_object(text: str) -> str | None:
    pos = text.rfind("}")
    if pos < 0:
        return None
    return text[: pos + 1] + "\n]"


def _variants(text: str):
    yield text
    fixed = (
        text.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )
    if fixed != text:
        yield fixed
    no_citation = re.sub(r"\[\s*citation\s*:\s*\d+\s*\]", "", text, flags=re.I)
    if no_citation != text:
        yield no_citation
    no_trailing = re.sub(r",\s*([}\]])", r"\1", fixed)
    if no_trailing != fixed:
        yield no_trailing
    trimmed = _trim_to_last_object(fixed)
    if trimmed:
        yield trimmed


def _try_parse(candidate: str) -> list[dict] | None:
    starts = [m.start() for m in re.finditer(r"\[", candidate)][:20]
    if not starts:
        starts = [0]
    for pos in starts:
        try:
            obj, _ = json.JSONDecoder().raw_decode(candidate[pos:])
        except Exception:
            continue
        if isinstance(obj, list):
            records = [x for x in obj if isinstance(x, dict)]
            if records:
                return records
        if isinstance(obj, dict):
            for key in ("data", "result", "results", "items", "records", "list"):
                value = obj.get(key)
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    return value
    return None


def _match_brace(text: str, start: int) -> int:
    depth = 0
    in_str = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return idx
    return -1


def _scan_objects(text: str) -> list[dict]:
    out: list[dict] = []
    pos = 0
    while True:
        start = text.find("{", pos)
        if start < 0:
            break
        end = _match_brace(text, start)
        if end < 0:
            break
        chunk = text[start : end + 1]
        try:
            obj = json.loads(chunk)
        except Exception:
            obj = None
        if isinstance(obj, dict) and obj:
            out.append(obj)
        pos = start + 1
    return out


def extract_json_array(text: str) -> list[dict]:
    if not text or not text.strip():
        raise ParseError("空回复")
    for block in _strip_fences(text):
        for variant in _variants(block):
            records = _try_parse(variant)
            if records is not None:
                return records
    records = _scan_objects(text)
    if records:
        return records
    raise ParseError("回复里找不到可解析的 JSON 数组")


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _is_content_path(path: str) -> bool:
    low = path.lower()
    if "thinking" in low or "reason" in low:
        return False
    return low == "content" or low.endswith("/content") or low.endswith(".content")


def parse_sse_content(raw: str) -> tuple[str, bool]:
    """解析 DeepSeek 的 SSE 流，返回（正文, 是否收到结束事件）。"""
    parts: list[str] = []
    finished = False
    event = ""
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal finished, event
        if event in ("finish", "close"):
            finished = True
        payload = "\n".join(data_lines).strip()
        data_lines.clear()
        if not payload:
            event = ""
            return
        try:
            obj = json.loads(payload)
        except Exception:
            event = ""
            return
        is_delta = event == "delta" or (
            not event and isinstance(obj, dict) and obj.get("p") and obj.get("o")
        )
        if is_delta:
            deltas = obj if isinstance(obj, list) else [obj]
            for delta in deltas:
                if not isinstance(delta, dict):
                    continue
                path = _as_text(delta.get("p"))
                op = _as_text(delta.get("o")).upper()
                value = delta.get("v")
                if not path or not _is_content_path(path):
                    continue
                if op == "APPEND":
                    parts.append(_as_text(value))
                elif op == "SET":
                    parts[:] = [_as_text(value)]
                elif op == "BATCH":
                    if isinstance(value, list):
                        parts.extend(_as_text(x) for x in value)
                    else:
                        parts.append(_as_text(value))
        if isinstance(obj, dict) and "choices" in obj:
            for choice in obj.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    parts.append(_as_text(delta["content"]))
                if choice.get("finish_reason"):
                    finished = True
        if isinstance(obj, dict) and obj.get("finish_reason") in ("stop", "length"):
            finished = True
        event = ""

    for line in raw.splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif not line.strip():
            flush()
    flush()
    return "".join(parts), finished


def normalize_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _as_text(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text.casefold()


def _name_from_record(record: dict) -> str:
    for key, value in record.items():
        low = str(key).lower()
        if "name" in low or "名称" in str(key) or "期刊" in str(key):
            if isinstance(value, str) and value.strip():
                return value
    return ""


def match_records(
    items, records: list[dict], match_field: str
) -> tuple[list[tuple[Any, dict]], list[Any]]:
    by_name: dict[str, dict] = {}
    for record in records:
        name = record.get(match_field)
        if not isinstance(name, str) or not name.strip():
            name = _name_from_record(record)
        key = normalize_name(name)
        if key:
            by_name.setdefault(key, record)

    pairs: list[tuple[Any, dict]] = []
    missing = []
    used: set[int] = set()
    same_length = len(records) == len(items)

    for index, item in enumerate(items):
        record = by_name.get(normalize_name(item.value))
        if record is None and same_length:
            candidate = records[index]
            if id(candidate) not in used:
                record = candidate
        if record is None or id(record) in used:
            missing.append(item)
            continue
        used.add(id(record))
        pairs.append((item, record))
    return pairs, missing


def normalize_record(record: dict, fields: list[str]) -> dict:
    out = dict(record)
    for field in fields:
        value = out.get(field, "")
        if value is None:
            value = ""
        elif not isinstance(value, str):
            value = str(value)
        value = re.sub(
            r"[\[【]\s*(?:citation|reference)\s*[:：]\s*\d+\s*[\]】]", "", value, flags=re.I
        ).strip()
        out[field] = value
    return out
