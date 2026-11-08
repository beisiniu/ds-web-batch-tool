from __future__ import annotations

import os
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.browser import BATCH_TOKEN_RE
from src.config import ConfigError, load_config
from src.inputs import Item, load_items
from src.main import _cookies_from_string, parse_session_blob
from src.web import build_preamble
from src.parse import (
    extract_json_array,
    match_records,
    normalize_record,
    parse_sse_content,
)
from src.prompts import build_batch_message
from src.store import Store


def test_real_excel():
    try:
        cfg = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    except ConfigError as exc:
        print("skip excel:", exc)
        return
    items, meta = load_items(cfg)
    assert len(items) == 10105, len(items)
    assert items[0].value == "21世纪"
    assert items[-1].value == "龙岩学院学报"
    assert meta["duplicates"] == 0
    assert cfg["batch"]["size"] == 50
    print("ok  excel:", len(items), "items,", -(-len(items) // 50), "batches")


def test_json_variants():
    sample_path = Path(os.environ.get("DS_SAMPLE_JSON", "数据.txt"))
    if not sample_path.exists():
        print("skip json sample: 数据.txt not found")
        return
    raw = sample_path.read_text(encoding="utf-8-sig")
    records = extract_json_array(raw)
    assert len(records) == 49
    assert list(records[0]) == ["journal_name", "official_url", "provider", "source_notes"]

    fenced = "结果如下：\n```json\n" + raw + "\n```\n完毕。"
    assert len(extract_json_array(fenced)) == 49

    truncated = raw[:1200]
    recovered = extract_json_array(truncated)
    assert 0 < len(recovered) < 49

    smart = '[{"journal_name":"测试刊","official_url":"https://a.com","provider":"自有官网","source_notes":"备注",},]'
    assert extract_json_array(smart)[0]["journal_name"] == "测试刊"

    with_citation = '[{"journal_name":"A[citation:1]","official_url":"https://a.com"}]'
    parsed = extract_json_array(with_citation)[0]
    assert parsed["journal_name"] == "A[citation:1]"
    assert normalize_record(parsed, ["journal_name"])["journal_name"] == "A"
    print("ok  json: plain/fenced/truncated/trailing/smart-quotes/citation")


def test_sse():
    part1 = '[{"journal_name":"A"},'
    part2 = '{"journal_name":"B"}]'
    delta1 = json.dumps(
        [{"p": "content", "o": "APPEND", "v": part1}], ensure_ascii=False
    )
    delta2 = json.dumps(
        {"p": "response/content", "o": "APPEND", "v": part2}, ensure_ascii=False
    )
    raw = (
        "event: ready\ndata: {\"response_message_id\":\"x\"}\n\n"
        f"event: delta\ndata: {delta1}\n\n"
        f"data: {delta2}\n\n"
        "event: finish\ndata: {}\n\n"
    )
    text, finished = parse_sse_content(raw)
    assert finished
    assert text == part1 + part2, text
    assert len(extract_json_array(text)) == 2
    print("ok  sse: content + finish + unnamed delta")


def test_matching():
    items = [Item("r2", "A", 2), Item("r3", "b", 3), Item("r4", "C", 4)]
    pairs, missing = match_records(
        items, [{"journal_name": "a"}, {"journal_name": "B"}], "journal_name"
    )
    assert [p[0].value for p in pairs] == ["A", "b"]
    assert [m.value for m in missing] == ["C"]

    pairs, missing = match_records(
        items,
        [{"journal_name": "x"}, {"journal_name": "y"}, {"journal_name": "z"}],
        "journal_name",
    )
    assert len(pairs) == 3, "same length should fall back to order"
    print("ok  match: by name + order fallback")


def test_store_resume():
    cfg = {
        "fields": ["journal_name", "official_url"],
        "output": {"json_name": "out.json", "csv_name": "out.csv"},
    }
    items = [Item("r2", "A", 2), Item("r3", "B", 3), Item("r4", "C", 4)]
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp), cfg)
        store.append(
            [
                {"id": "r2", "status": "ok", "record": {"journal_name": "A", "official_url": "https://a"}},
                {"id": "r3", "status": "failed", "error": "parse"},
            ]
        )
        counts = store.counts(items)
        assert counts == {"ok": 1, "failed": 1, "pending": 1, "total": 3}

        store.append([{"id": "r3", "status": "retry"}])
        assert not store.is_done("r3")
        assert store.counts(items)["pending"] == 2

        store.append(
            [{"id": "r3", "status": "ok", "record": {"journal_name": "B", "official_url": "https://b"}}]
        )
        result = store.export(items, cfg["fields"], "out.json", "out.csv")
        data = json.loads(Path(tmp, "out.json").read_text(encoding="utf-8"))
        assert len(data) == 2
        assert data[0]["journal_name"] == "A"
        assert result["missing_count"] == 1
        assert Path(tmp, "missing.csv").exists()
        print("ok  store: resume/retry/export/missing")


def test_prompts():
    cfg = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    items = [Item("r2", "21世纪", 2), Item("r3", "Ambio-人类环境杂志", 3)]
    message = build_batch_message(cfg, items, 3, 10105, 2)
    assert "第 3 批" in message
    assert "1. 21世纪" in message
    assert "2. Ambio-人类环境杂志" in message
    assert "批次校验码" in message
    assert BATCH_TOKEN_RE.search(message)
    assert "{batch_no}" not in message
    print("ok  prompt: batch message rendered with batch token")


def test_session_parse():
    local, session, cookies = parse_session_blob(
        '{"local":{"userToken":"tok","a":"1"},"session":{"s":"2"},"cookies":"c=3; d=4"}'
    )
    assert local["userToken"] == "tok"
    assert local["a"] == "1"
    assert session["s"] == "2"
    assert len(_cookies_from_string(cookies)) == 2

    local, _, _ = parse_session_blob('{"userToken":"tok"}')
    assert local["userToken"] == "tok"

    _, _, cookies = parse_session_blob("cookie: a=1; b=2\n")
    assert "a=1" in cookies

    local, _, _ = parse_session_blob("tok_" + "x" * 30)
    assert local["userToken"].startswith("tok_")
    print("ok  session: JSON / flat localStorage / cookie header / raw token")


def test_preamble_build():
    fields = [
        {"key": "journal_name", "desc": "原样保留"},
        {"key": "official_url", "desc": ""},
    ]
    journal = build_preamble("journal", fields, "journal_name", notes="优先官网")
    assert "期刊" in journal
    assert "journal_name" in journal
    assert "{batch_size}" in journal
    assert "补充要求" in journal and "优先官网" in journal
    assert "只输出一个 JSON 数组" in journal
    assert "不要输出解释" in journal
    assert "绝对不要编造" in journal

    custom = build_preamble(
        "custom", fields, "journal_name", custom_prompt="查这批期刊的官网和收录平台"
    )
    assert custom.startswith("查这批期刊的官网和收录平台")
    assert "请严格按以下字段输出" in custom
    assert "输出格式（必须遵守）" in custom
    assert "official_url" in custom

    scholar = build_preamble("scholar", [{"key": "name", "desc": "姓名"}], "name")
    assert "学者" in scholar
    assert "原样保留" in scholar
    print("ok  preamble: journal/scholar/custom + fixed format rules")


if __name__ == "__main__":
    test_real_excel()
    test_json_variants()
    test_sse()
    test_matching()
    test_store_resume()
    test_prompts()
    test_session_parse()
    test_preamble_build()
    print("\nALL OFFLINE TESTS PASSED")
