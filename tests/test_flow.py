from __future__ import annotations

import json
import logging
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.browser import RateLimitError, Reply, StuckError
from src.inputs import Item
from src.main import StopRun, _run_loop
from src.store import Store


class FakeBrowser:
    def __init__(self, fail_labels=(), drop_labels=()):
        self.url = "https://chat.deepseek.com/a/chat/s/c1"
        self.chat_seq = 1
        self.new_chat_calls = 0
        self.accounts = [{"name": "默认账号", "profile_dir": Path(".")}]
        self.calls = []
        self.attempts = {}
        self.fail_labels = set(fail_labels)
        self.drop_labels = set(drop_labels)

    def current_account_name(self):
        return "默认账号"

    def has_multiple_accounts(self):
        return False

    def switch_account(self):
        return False

    def seconds_until_account(self):
        return 0

    def use_account(self, name):
        pass

    def new_chat(self):
        self.new_chat_calls += 1
        self.chat_seq += 1
        self.url = f"https://chat.deepseek.com/a/chat/s/c{self.chat_seq}"

    def goto_conversation(self, url):
        self.url = url

    @property
    def current_url(self):
        return self.url

    def cancel_generation(self):
        pass

    def ask(self, text, label="", force_resend=False):
        self.calls.append((label, text))
        if "下面是第" not in text:
            return Reply("好的", "好的", "", self.url)
        names = re.findall(r"^\d+\. (.+)$", text, re.M)
        self.attempts[label] = self.attempts.get(label, 0) + 1
        attempt = self.attempts[label]
        if label in self.fail_labels and attempt == 1:
            return Reply("这不是 JSON", "这不是 JSON", "", self.url)
        records = [
            {"journal_name": name, "official_url": f"https://example.com/{index}"}
            for index, name in enumerate(names)
        ]
        if label in self.drop_labels and attempt == 1 and records:
            records = records[:-1]
        return Reply(json.dumps(records, ensure_ascii=False), "", "", self.url)


def make_cfg(size=5, rotate=2, max_attempts=2, split=True):
    return {
        "batch": {
            "size": size,
            "rotate_every_batches": rotate,
            "max_attempts": max_attempts,
            "split_on_parse_failure": split,
            "cooldown_sec": [0, 0],
        },
        "deepseek": {
            "conversation_url": "",
            "send_preamble": "auto",
            "response_timeout_sec": 10,
            "login_wait_sec": 1,
        },
        "network": {
            "rate_limit_backoff_sec": [0],
            "max_rate_limit_retries": 0,
            "max_total_cooldown_sec": 1,
        },
        "fields": ["journal_name", "official_url"],
        "match_field": "journal_name",
        "preamble": "任务说明 {batch_size}",
        "batch_header": "下面是第 {batch_no} 批，共 {count} 个（总 {total}）：",
        "retry_note": "请重新输出 JSON。",
        "output": {"json_name": "out.json", "csv_name": "out.csv"},
    }


def make_items(n):
    return [Item(f"r{i + 2}", f"名称{i + 1}", i + 2) for i in range(n)]


def run_flow(tmp, cfg, items, browser):
    logger = logging.getLogger("flow")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    store = Store(Path(tmp), cfg)
    state = {
        "total_items": len(items),
        "batch_counter": 0,
        "batches_in_conversation": 0,
        "preamble_sent": False,
        "conversation_url": "",
    }
    _run_loop(browser, store, cfg, state, logger, list(items), items)
    return store, state


def test_all_ok_and_rotation():
    items = make_items(12)
    browser = FakeBrowser()
    with tempfile.TemporaryDirectory() as tmp:
        store, state = run_flow(tmp, make_cfg(size=5, rotate=2), items, browser)
        counts = store.counts(items)
        assert counts["ok"] == 12, counts
        assert state["batches_in_conversation"] == 1
        preambles = [c for c in browser.calls if "下面是第" not in c[1]]
        assert len(preambles) == 2, len(preambles)
        assert browser.new_chat_calls == 2, browser.new_chat_calls
        result = store.export(items, ["journal_name", "official_url"], "out.json", "out.csv")
        data = json.loads(Path(tmp, "out.json").read_text(encoding="utf-8"))
        assert len(data) == 12
        assert result["missing_count"] == 0
    print("ok  flow: 12 items, retry-free, rotation + preamble")


def test_missing_item_retry():
    items = make_items(6)
    browser = FakeBrowser(drop_labels={"b0001"})
    with tempfile.TemporaryDirectory() as tmp:
        store, state = run_flow(tmp, make_cfg(size=6, rotate=0), items, browser)
        assert store.counts(items)["ok"] == 6
        batch_calls = [c for c in browser.calls if "下面是第" in c[1]]
        assert len(batch_calls) == 2, len(batch_calls)
        assert "名称6" in batch_calls[1][1]
    print("ok  flow: missing record retried with a smaller follow-up batch")


def test_parse_failure_retry():
    items = make_items(5)
    browser = FakeBrowser(fail_labels={"b0001"})
    with tempfile.TemporaryDirectory() as tmp:
        store, _ = run_flow(tmp, make_cfg(size=5, rotate=0), items, browser)
        assert store.counts(items)["ok"] == 5
        batch_calls = [c for c in browser.calls if "下面是第" in c[1]]
        assert len(batch_calls) == 2
        assert "请重新输出 JSON" in batch_calls[1][1]
    print("ok  flow: parse failure retried with stricter note")


def test_split_on_parse_failure():
    items = make_items(12)
    browser = FakeBrowser(fail_labels={"b0001"})
    with tempfile.TemporaryDirectory() as tmp:
        store, _ = run_flow(tmp, make_cfg(size=12, rotate=0, split=True), items, browser)
        assert store.counts(items)["ok"] == 12
        labels = [c[0] for c in browser.calls if "下面是第" in c[1]]
        assert "b0001a" in labels and "b0001b" in labels, labels
    print("ok  flow: large batch split after parse failure")


class RateLimitBrowser(FakeBrowser):
    def __init__(self, fails):
        super().__init__()
        self.fails = fails

    def ask(self, text, label="", force_resend=False):
        if label.startswith("b") and self.fails > 0:
            self.fails -= 1
            raise RateLimitError("服务器繁忙")
        return super().ask(text, label, force_resend=force_resend)


def test_rate_limit_retry():
    items = make_items(3)
    browser = RateLimitBrowser(2)
    cfg = make_cfg(size=3, rotate=0)
    cfg["network"]["rate_limit_backoff_sec"] = [0, 0]
    cfg["network"]["max_rate_limit_retries"] = 2
    cfg["network"]["max_total_cooldown_sec"] = 100
    with tempfile.TemporaryDirectory() as tmp:
        store, _ = run_flow(tmp, cfg, items, browser)
        assert store.counts(items)["ok"] == 3
        assert browser.fails == 0
    print("ok  flow: rate limit retried, then saved")


def test_rate_limit_abort_keeps_pending():
    items = make_items(3)
    browser = RateLimitBrowser(99)
    cfg = make_cfg(size=3, rotate=0)
    cfg["network"]["rate_limit_backoff_sec"] = [0, 0]
    cfg["network"]["max_rate_limit_retries"] = 2
    cfg["network"]["max_total_cooldown_sec"] = 100
    with tempfile.TemporaryDirectory() as tmp:
        try:
            run_flow(tmp, cfg, items, browser)
            raise AssertionError("should have stopped")
        except StopRun:
            pass
        store = Store(Path(tmp), cfg)
        counts = store.counts(items)
        assert counts == {"ok": 0, "failed": 0, "pending": 3, "total": 3}, counts
        assert store.load_state().get("total_items") == 3
    print("ok  flow: rate limit exhaustion keeps items pending + state saved")


class SwitchFakeBrowser(FakeBrowser):
    def __init__(self):
        super().__init__()
        self.accounts = [
            {"name": "主账号", "profile_dir": Path(".")},
            {"name": "备用账号", "profile_dir": Path(".")},
        ]
        self.index = 0
        self.switched = 0
        self.stuck_once = False

    def current_account_name(self):
        return self.accounts[self.index]["name"]

    def has_multiple_accounts(self):
        return True

    def switch_account(self):
        self.index = 1 - self.index
        self.switched += 1
        self.chat_seq += 1
        self.url = f"https://chat.deepseek.com/a/chat/s/c{self.chat_seq}"
        return True

    def seconds_until_account(self):
        return 0

    def ask(self, text, label="", force_resend=False):
        if "下面是第" in text and self.index == 0 and not self.stuck_once:
            self.stuck_once = True
            raise StuckError("stuck test")
        return super().ask(text, label, force_resend=force_resend)


def test_account_switch_on_stuck():
    items = make_items(6)
    browser = SwitchFakeBrowser()
    cfg = make_cfg(size=6, rotate=0)
    cfg["accounts"] = {
        "enabled": True,
        "stuck_switch_sec": 1,
        "account_cooldown_sec": 0,
        "switch_on_rate_limit_after": 0,
        "profiles": browser.accounts,
    }
    with tempfile.TemporaryDirectory() as tmp:
        store, state = run_flow(tmp, cfg, items, browser)
        assert store.counts(items)["ok"] == 6
        assert browser.switched == 1
        assert state["active_account"] == "备用账号"
        preambles = [call for call in browser.calls if "下面是第" not in call[1]]
        assert len(preambles) >= 2, len(preambles)
    print("ok  flow: stuck account switches, re-sends preamble, keeps progress")


def test_resume():
    items = make_items(12)
    with tempfile.TemporaryDirectory() as tmp:
        browser = FakeBrowser()
        cfg = make_cfg(size=5, rotate=0)
        store, state = run_flow(tmp, cfg, items, browser)
        assert store.counts(items)["ok"] == 12

        store2 = Store(Path(tmp), cfg)
        pending = [item for item in items if not store2.is_done(item.id)]
        assert pending == []
        state2 = store2.load_state()
        assert state2.get("total_items") == 12
    print("ok  flow: restart sees no pending work")


if __name__ == "__main__":
    test_all_ok_and_rotation()
    test_missing_item_retry()
    test_parse_failure_retry()
    test_split_on_parse_failure()
    test_rate_limit_retry()
    test_rate_limit_abort_keeps_pending()
    test_account_switch_on_stuck()
    test_resume()
    print("\nALL FLOW TESTS PASSED")
