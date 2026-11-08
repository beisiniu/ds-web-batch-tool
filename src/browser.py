from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

from .parse import parse_sse_content

BASE_URL = "https://chat.deepseek.com/"
SIGN_IN_URL = "https://chat.deepseek.com/sign_in"
COMPLETION_PATH = "/api/v0/chat/completion"

MARK_INPUT_JS = """
() => {
  document.querySelectorAll('[data-ds-auto-input]').forEach(e => e.removeAttribute('data-ds-auto-input'));
  const els = [...document.querySelectorAll('textarea, div[contenteditable="true"]')];
  const candidates = els.map(el => ({el, r: el.getBoundingClientRect()}))
    .filter(x => x.r.width > 280 && x.r.height > 12 && x.r.bottom > innerHeight * 0.45 && x.r.top < innerHeight);
  candidates.sort((a, b) => (b.r.width * b.r.height) - (a.r.width * a.r.height));
  if (!candidates.length) return false;
  candidates[0].el.setAttribute('data-ds-auto-input', '1');
  return true;
}
"""

READ_LAST_JS = """
() => {
  const nodes = [...document.querySelectorAll('.ds-markdown')];
  if (!nodes.length) return {code: '', full: ''};
  const el = nodes[nodes.length - 1];
  let codes = [...el.querySelectorAll('pre code')].map(c => c.textContent || '');
  if (!codes.length) codes = [...el.querySelectorAll('pre')].map(c => c.textContent || '');
  codes.sort((a, b) => b.length - a.length);
  return {code: codes.join('\\n\\n'), full: el.innerText || ''};
}
"""

ERROR_WORDS = [
    "服务器繁忙",
    "请求过于频繁",
    "请求超时",
    "网络异常",
    "发送失败",
    "内容过长",
    "请稍后再试",
    "达到上限",
    "too many requests",
    "rate limit",
    "server busy",
]

VERIFY_WORDS = [
    "点击图中",
    "请完成验证",
    "安全验证",
    "拖动滑块",
    "人机验证",
    "完成验证",
    "请验证",
    "验证失败",
]

BATCH_TOKEN_RE = re.compile(r"批次校验码\s*([0-9a-fA-F]{6,16})")


class BrowserError(RuntimeError):
    pass


class RateLimitError(BrowserError):
    pass


class LoginRequired(BrowserError):
    pass


class ResponseTimeout(BrowserError):
    pass


class StuckError(BrowserError):
    pass


class SwitchAccountError(BrowserError):
    pass


@dataclass
class Reply:
    dom_code: str
    dom_full: str
    sse: str
    url: str


class DeepSeekBrowser:
    def __init__(self, cfg: dict, profile_dir: Path, logger: logging.Logger):
        self.cfg = cfg
        accounts_cfg = cfg.get("accounts") or {}
        root = Path(str(cfg.get("_root") or "."))
        self.accounts = []
        for index, item in enumerate(accounts_cfg.get("profiles") or []):
            path = Path(str(item.get("profile_dir") or ""))
            if not path.is_absolute():
                path = root / path
            self.accounts.append(
                {"name": str(item.get("name") or f"账号{index + 1}"), "profile_dir": path}
            )
        if not self.accounts:
            self.accounts = [{"name": "默认账号", "profile_dir": Path(profile_dir)}]
        self.accounts_cfg = accounts_cfg
        self.account_index = 0
        self._account_cooldowns: dict[int, float] = {}
        self.profile_dir = self.accounts[0]["profile_dir"]
        self.logger = logger
        self.playwright = None
        self.context = None
        self.page = None
        self.channel = "chrome"
        self.stable_sec = 8.0
        self._finished_request = None
        self._finished_at: float | None = None
        self._failed_at: float | None = None
        self._failed_error = ""
        self._sent_at = 0.0

    def _close_stale_chrome(self) -> None:
        if not (self.cfg.get("browser") or {}).get("close_stale", True):
            return
        try:
            import psutil
        except ImportError:
            return
        target = str(self.profile_dir).lower()
        killed = 0
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if name not in ("chrome.exe", "msedge.exe"):
                    continue
                cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                if target in cmdline and "crashpad" not in cmdline:
                    proc.kill()
                    killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if killed:
            self.logger.info(f"检测到 {killed} 个上次残留的脚本浏览器进程，已自动关闭")
            time.sleep(1.5)

    def _launch_active_profile(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._close_stale_chrome()
        browser_cfg = self.cfg.get("browser") or {}
        args = [
            "--disable-blink-features=AutomationControlled",
            "--start-maximized",
        ]
        if browser_cfg.get("no_proxy", True):
            args.append("--no-proxy-server")
        last_error = None
        preferred = str(browser_cfg.get("channel", "auto")).lower()
        channels = ("msedge", "chrome") if preferred == "msedge" else ("chrome", "msedge")
        self.context = None
        for channel in channels:
            try:
                self.context = self.playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    channel=channel,
                    headless=False,
                    no_viewport=True,
                    locale="zh-CN",
                    chromium_sandbox=True,
                    args=args,
                )
                self.channel = channel
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        if self.context is None:
            raise BrowserError(
                f"无法启动 Chrome/Edge：{last_error}\n"
                "如果之前脚本打开的 Chrome 窗口还没关，请先把它关掉再试。"
            )

        self.context.set_default_timeout(30_000)
        pages = self.context.pages
        self.page = pages[0] if pages else self.context.new_page()
        self.page.on("requestfinished", self._on_request_finished)
        self.page.on("requestfailed", self._on_request_failed)
        self._finished_request = None
        self._finished_at = None
        self._failed_at = None
        self._failed_error = ""
        self._sent_at = 0.0

    def start(
        self,
        require_login: bool = True,
        timeout: int | None = None,
        prefer_login_page: bool = False,
    ) -> None:
        self.playwright = sync_playwright().start()
        self._launch_active_profile()
        browser_cfg = self.cfg.get("browser") or {}
        proxy_note = "（已绕过系统代理）" if browser_cfg.get("no_proxy", True) else ""
        self.logger.info(
            f"浏览器已启动（{self.channel}）{proxy_note}，账号：{self.current_account_name()}，正在打开 DeepSeek..."
        )
        start_url = SIGN_IN_URL if prefer_login_page else BASE_URL
        self.page.goto(start_url, wait_until="domcontentloaded")
        if require_login:
            self._wait_for_chat(int(timeout or self.cfg["deepseek"]["login_wait_sec"]))

    def current_account_name(self) -> str:
        return self.accounts[self.account_index]["name"]

    def has_multiple_accounts(self) -> bool:
        return len(self.accounts) > 1

    def use_account(self, name: str) -> None:
        if not name:
            return
        for index, account in enumerate(self.accounts):
            if account["name"] == name:
                self.account_index = index
                self.profile_dir = account["profile_dir"]
                return

    def _next_available_account(self) -> int | None:
        now = time.time()
        for offset in range(1, len(self.accounts) + 1):
            index = (self.account_index + offset) % len(self.accounts)
            if self._account_cooldowns.get(index, 0) <= now:
                return index
        return None

    def seconds_until_account(self) -> float:
        now = time.time()
        waits = [
            until - now
            for index, until in self._account_cooldowns.items()
            if index != self.account_index and until > now
        ]
        return max(0.0, min(waits)) if waits else 0.0

    def switch_account(self) -> bool:
        if not self.has_multiple_accounts():
            return False
        cooldown = float(self.accounts_cfg.get("account_cooldown_sec", 600) or 0)
        if cooldown > 0:
            self._account_cooldowns[self.account_index] = time.time() + cooldown
        index = self._next_available_account()
        if index is None:
            return False
        previous = self.current_account_name()
        self.account_index = index
        self.profile_dir = self.accounts[index]["profile_dir"]
        self.logger.warning(f"切换账号：{previous} → {self.current_account_name()}")
        try:
            if self.context is not None:
                self.context.close()
        except PWError:
            pass
        self.context = None
        self._launch_active_profile()
        self.page.goto(BASE_URL, wait_until="domcontentloaded")
        self._wait_for_chat(int(self.cfg["deepseek"]["login_wait_sec"]))
        return True

    def close(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.playwright is not None:
                self.playwright.stop()
        except Exception:  # noqa: BLE001
            pass

    def _on_request_finished(self, request) -> None:
        try:
            if COMPLETION_PATH in request.url and request.method == "POST":
                self._finished_request = request
                self._finished_at = time.time()
        except Exception:  # noqa: BLE001
            pass

    def _on_request_failed(self, request) -> None:
        try:
            if COMPLETION_PATH in request.url and request.method == "POST":
                self._failed_at = time.time()
                self._failed_error = str(request.failure or "")
        except Exception:  # noqa: BLE001
            pass

    @property
    def current_url(self) -> str:
        if not self.page:
            return ""
        url = self.page.url
        return url if "/a/chat/s/" in url else ""

    def _on_login_page(self) -> bool:
        url = self.page.url.lower()
        return "sign_in" in url or "/login" in url

    def _input_locator(self):
        if not self.page or self._on_login_page():
            return None
        for placeholder in ("给 DeepSeek 发送消息", "Message DeepSeek"):
            loc = self.page.get_by_placeholder(placeholder, exact=False)
            try:
                for index in range(loc.count()):
                    candidate = loc.nth(index)
                    if candidate.is_visible():
                        return candidate
            except PWError:
                continue
        try:
            if self.page.evaluate(MARK_INPUT_JS):
                loc = self.page.locator('[data-ds-auto-input="1"]')
                if loc.count() and loc.first.is_visible():
                    return loc.first
        except PWError:
            pass
        return None

    def _wait_for_chat(self, timeout: int) -> None:
        start = time.time()
        warned = False
        while time.time() - start < timeout:
            if self._input_locator() is not None:
                return
            if not warned:
                self.logger.warning(
                    "没有找到聊天输入框。如果页面要求登录，请在弹出的 Chrome 窗口里完成登录；脚本会继续等待。"
                )
                warned = True
            self.page.wait_for_timeout(3000)
        raise LoginRequired("等待登录/页面加载超时，请先运行「首次登录」")

    def _assistant_count(self) -> int:
        try:
            return self.page.locator(".ds-markdown").count()
        except PWError:
            return 0

    def _read_last(self) -> dict:
        try:
            return self.page.evaluate(READ_LAST_JS) or {"code": "", "full": ""}
        except PWError:
            return {"code": "", "full": ""}

    def _stop_visible(self) -> bool:
        try:
            return self.page.locator('[aria-label="停止生成"], [aria-label="Stop"]').first.is_visible()
        except PWError:
            return False

    def _detect_error(self) -> str:
        try:
            text = self.page.locator("body").inner_text()
        except PWError:
            return ""
        tail = text[-3000:]
        for word in ERROR_WORDS:
            if word in tail:
                return f"页面提示「{word}」"
        return ""

    def _raise_if_error(self, message: str) -> None:
        low = message.lower()
        if any(
            word in low
            for word in ("繁忙", "频繁", "稍后", "超时", "上限", "too many", "rate limit", "server busy")
        ):
            raise RateLimitError(message)
        raise BrowserError(message)

    def _verification_visible(self) -> bool:
        if self._on_login_page():
            return True
        texts = []
        try:
            texts.append(self.page.locator("body").inner_text())
        except PWError:
            return False
        for frame in self.page.frames:
            if frame is self.page.main_frame:
                continue
            try:
                texts.append(frame.locator("body").inner_text())
            except PWError:
                continue
        tail = "\n".join(texts)[-4000:]
        low = tail.lower()
        if any(word in tail for word in VERIFY_WORDS):
            return True
        return "verify you are human" in low

    def _wait_for_human_verification(self) -> None:
        limit = float(self.cfg["deepseek"].get("verification_wait_sec", 900))
        start = time.time()
        warned = False
        while self._verification_visible():
            if time.time() - start > limit:
                raise LoginRequired(
                    f"等待安全验证超过 {int(limit)} 秒，请完成验证后再运行，进度不会丢"
                )
            if not warned:
                self.logger.warning(
                    "检测到 DeepSeek 安全验证/登录页，请在脚本浏览器里完成；"
                    "脚本已暂停发送，不会重复提交本批。"
                )
                warned = True
            self.page.wait_for_timeout(2000)
        if warned:
            self.logger.info("安全验证已通过，继续本批。")
            self.page.wait_for_timeout(1000)

    def _extract_batch_token(self, text: str) -> str:
        match = BATCH_TOKEN_RE.search(text or "")
        return match.group(1) if match else ""

    def _existing_answer_for_token(self, token: str) -> str:
        if not token:
            return ""
        try:
            return self.page.evaluate(
                """(token) => {
                  const needle = '批次校验码 ' + token;
                  const all = [...document.querySelectorAll('div, span, p')];
                  const hits = all.filter(el => (el.textContent || '').includes(needle));
                  if (!hits.length) return '';
                  const inner = hits.filter(el => ![...el.children].some(c => (c.textContent || '').includes(needle)));
                  const markers = inner.length ? inner : hits;
                  const marker = markers[markers.length - 1];
                  const msgs = [...document.querySelectorAll('.ds-markdown')];
                  const after = msgs.filter(m => marker.compareDocumentPosition(m) & Node.DOCUMENT_POSITION_FOLLOWING);
                  return after.length ? (after[after.length - 1].innerText || '') : '';
                }""",
                token,
            ) or ""
        except PWError:
            return ""

    def new_chat(self) -> None:
        for _ in range(4):
            if self._assistant_count() == 0 and self._input_locator() is not None:
                return
            if self._verification_visible():
                self._wait_for_human_verification()
            try:
                for text in ("开启新对话", "New chat"):
                    button = self.page.get_by_text(text, exact=True)
                    if button.count():
                        button.first.click(timeout=5000)
                        break
                else:
                    self.page.goto(BASE_URL, wait_until="domcontentloaded")
                deadline = time.time() + 15
                while time.time() < deadline:
                    if self._verification_visible():
                        self._wait_for_human_verification()
                    if self._assistant_count() == 0 and self._input_locator() is not None:
                        return
                    self.page.wait_for_timeout(1000)
            except PWError:
                self.page.wait_for_timeout(1000)
        raise BrowserError("无法新建会话，请检查网页是否正常")

    def goto_conversation(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded")
        self._wait_for_chat(60)
        if self._assistant_count() == 0:
            self.page.wait_for_timeout(4000)
        if self._input_locator() is None:
            raise BrowserError(f"会话打不开：{url}")

    def cancel_generation(self) -> None:
        try:
            stop = self.page.locator('[aria-label="停止生成"], [aria-label="Stop"]').first
            if stop.is_visible():
                stop.click()
                self.logger.warning("已点击「停止生成」，准备重试")
                self.page.wait_for_timeout(1500)
        except PWError:
            pass

    def ask(self, text: str, label: str = "", force_resend: bool = False) -> Reply:
        if self._verification_visible():
            self._wait_for_human_verification()
        if not force_resend:
            token = self._extract_batch_token(text)
            if token:
                existing = self._existing_answer_for_token(token)
                if existing and "[" in existing and "]" in existing:
                    self.logger.info("检测到本批在对话里已有回答，直接复用，不重复发送")
                    return Reply(dom_code=existing, dom_full=existing, sse="", url=self.current_url)
        if self._input_locator() is None:
            self._wait_for_chat(int(self.cfg["deepseek"]["login_wait_sec"]))
        before = self._assistant_count()
        before_last = self._read_last()
        before_text = before_last.get("code") or before_last.get("full") or ""
        self._finished_request = None
        self._finished_at = None
        self._failed_at = None
        self._failed_error = ""

        loc = self._input_locator()
        if loc is None:
            raise BrowserError("找不到输入框")
        loc.fill(text)
        self.page.wait_for_timeout(250)
        self._sent_at = time.time()
        loc.press("Enter")
        self._wait_sent(loc, before)

        dom = self._wait_answer(before, text, before_text)
        sse = ""
        if self._finished_request is not None:
            try:
                response = self._finished_request.response()
                if response is not None:
                    status = response.status
                    body = response.text()
                    if status == 429 or status == 503:
                        raise RateLimitError(f"接口返回 HTTP {status}")
                    if status >= 400:
                        self.logger.warning(f"回复接口返回 HTTP {status}")
                    sse, finished = parse_sse_content(body)
                    if sse and not finished:
                        sse = ""
            except RateLimitError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.logger.debug(f"读取 SSE 失败：{exc}")

        content = dom.get("code") or dom.get("full") or sse
        if content and len(content) < 300:
            for word in ERROR_WORDS:
                if word in content:
                    raise RateLimitError(f"AI 回复了异常提示：{content.strip()[:80]}")

        return Reply(
            dom_code=dom.get("code", ""),
            dom_full=dom.get("full", ""),
            sse=sse,
            url=self.current_url,
        )

    def _wait_sent(self, loc, before: int) -> None:
        end = time.time() + 10
        while time.time() < end:
            if self._verification_visible():
                self._wait_for_human_verification()
                end = time.time() + 10
                continue
            try:
                value = loc.input_value()
            except PWError:
                try:
                    value = loc.inner_text()
                except PWError:
                    value = ""
            if not value.strip():
                return
            if self._assistant_count() > before or self._stop_visible():
                return
            self.page.wait_for_timeout(500)
        try:
            loc.press("Enter")
        except PWError:
            pass
        self.page.wait_for_timeout(2000)
        try:
            if not loc.input_value().strip():
                return
        except PWError:
            pass
        if self._click_send_button():
            self.page.wait_for_timeout(2000)
            try:
                if not loc.input_value().strip():
                    return
            except PWError:
                pass
        raise BrowserError("消息没有发送出去，请检查网页状态")

    def _click_send_button(self) -> bool:
        try:
            point = self.page.evaluate(
                """
                () => {
                  const els = [...document.querySelectorAll('button, [role="button"]')];
                  const candidates = els.map(el => ({el, r: el.getBoundingClientRect()}))
                    .filter(x => x.r.width > 0 && x.r.height > 0 &&
                                 x.r.top > innerHeight * 0.55 && x.r.left > innerWidth * 0.6);
                  candidates.sort((a, b) => (b.r.bottom - a.r.bottom) || (b.r.right - a.r.right));
                  if (!candidates.length) return null;
                  const r = candidates[0].r;
                  return {x: r.x + r.width / 2, y: r.y + r.height / 2};
                }
                """
            )
        except PWError:
            return False
        if not point:
            return False
        self.page.mouse.click(point["x"], point["y"])
        return True

    def _wait_answer(self, before: int, sent_text: str = "", before_text: str = "") -> dict:
        timeout = float(self.cfg["deepseek"]["response_timeout_sec"])
        start = time.time()
        paused_total = 0.0
        last_text = ""
        last_change = start
        last_log = start
        result = {"code": "", "full": ""}
        sent_normalized = sent_text.strip()
        before_normalized = (before_text or "").strip()
        stuck_after = (
            float(self.accounts_cfg.get("stuck_switch_sec", 0) or 0)
            if self.has_multiple_accounts()
            else 0.0
        )

        while time.time() - start - paused_total < timeout:
            if self._verification_visible():
                pause_start = time.time()
                self._wait_for_human_verification()
                paused_total += time.time() - pause_start
                last_change = time.time()
                continue
            count = self._assistant_count()
            result = self._read_last()
            text = result.get("code") or result.get("full") or ""
            if text.strip() == sent_normalized:
                text = ""
            changed = count > before or (bool(text) and text.strip() != before_normalized)
            if text and changed:
                if text != last_text:
                    last_text = text
                    last_change = time.time()
                finished = self._finished_at is not None and self._finished_at > self._sent_at
                stable = time.time() - last_change
                if not self._stop_visible() and (finished or stable >= self.stable_sec):
                    self.page.wait_for_timeout(1200)
                    final = self._read_last()
                    final_text = final.get("code") or final.get("full") or ""
                    if final_text.strip() == sent_normalized:
                        self.page.wait_for_timeout(1000)
                        continue
                    return final
            if stuck_after and time.time() - last_change > stuck_after:
                raise StuckError(
                    f"回答超过 {int(stuck_after)} 秒没有进展（页面生成中或内容未稳定）"
                )
            if time.time() - last_log >= 30:
                elapsed = int(time.time() - start - paused_total)
                self.logger.info(f"仍在等待回答... 已 {elapsed} 秒（页面生成中或内容未稳定）")
                last_log = time.time()
            if time.time() - last_change > 20:
                error = self._detect_error()
                if error:
                    self._raise_if_error(error)
            if time.time() - start - paused_total > 60 and not last_text:
                if self._failed_at and self._failed_at > start:
                    raise RateLimitError(f"请求失败：{self._failed_error}")
            self.page.wait_for_timeout(1000)

        error = self._detect_error()
        if error:
            self._raise_if_error(error)
        raise ResponseTimeout(f"等待回复超过 {int(timeout)} 秒")
