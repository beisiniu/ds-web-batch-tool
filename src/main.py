from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from playwright.sync_api import Error as PWError

from .browser import (
    BrowserError,
    DeepSeekBrowser,
    LoginRequired,
    RateLimitError,
    ResponseTimeout,
    StuckError,
    SwitchAccountError,
)
from .config import ConfigError, load_config, output_dir
from .inputs import InputError, load_items
from .parse import ParseError, extract_json_array, match_records, normalize_record
from .prompts import build_batch_message, render
from .store import Store


class StopRun(RuntimeError):
    pass


def setup_logger(outdir: Path) -> logging.Logger:
    outdir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("dsbatch")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
    file_handler = logging.FileHandler(outdir / "run.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


def profile_path(cfg: dict) -> Path:
    name = (cfg.get("browser") or {}).get("profile_dir") or "browser-profile"
    path = Path(name)
    return path if path.is_absolute() else Path(cfg["_root"]) / path


def _storage_value(value) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _extract_cookie_header(text: str) -> str:
    match = re.search(r"(?im)^\s*cookie:\s*(.+)$", text)
    if match:
        return match.group(1).strip()
    match = re.search(r"""(?:-H|--header)\s+(['"])cookie:\s*(.*?)\1""", text, re.I | re.S)
    if match:
        return match.group(2).strip()
    match = re.search(r"""(?:-b|--cookie)\s+(['"])(.*?)\1""", text, re.I | re.S)
    if match:
        return match.group(2).strip()
    return ""


def parse_session_blob(text: str) -> tuple[dict, dict, str]:
    text = (text or "").strip().lstrip("\ufeff")
    if not text:
        raise ConfigError("剪贴板是空的。请先在已登录浏览器里复制 localStorage。")

    try:
        obj = json.loads(text)
    except Exception:
        obj = None

    if isinstance(obj, str):
        return {"userToken": obj}, {}, ""

    if isinstance(obj, dict):
        local = obj.get("local") or obj.get("localStorage") or obj.get("local_storage") or {}
        session = obj.get("session") or obj.get("sessionStorage") or obj.get("session_storage") or {}
        cookies = obj.get("cookies") or obj.get("cookie") or ""
        if not local and not session:
            local = {
                key: value
                for key, value in obj.items()
                if key not in ("cookies", "cookie", "token")
            }
            if "token" in obj and "userToken" not in local:
                local["userToken"] = obj["token"]
        if isinstance(cookies, dict):
            cookies = "; ".join(f"{key}={value}" for key, value in cookies.items())
        if local or session or cookies:
            local = {str(k): _storage_value(v) for k, v in (local or {}).items() if v is not None}
            session = {str(k): _storage_value(v) for k, v in (session or {}).items() if v is not None}
            return local, session, str(cookies)

    cookie_text = _extract_cookie_header(text)
    if cookie_text:
        return {}, {}, cookie_text

    if "\n" not in text and " " not in text and len(text) >= 20:
        return {"userToken": text}, {}, ""

    raise ConfigError("没识别出 token。请复制 localStorage（推荐），或请求头里的 cookie。")


def _cookies_from_string(cookie_text: str) -> list[dict]:
    out = []
    for part in cookie_text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "value": value.strip(),
                "domain": "chat.deepseek.com" if name.startswith("__Host-") else ".deepseek.com",
                "path": "/",
                "secure": True,
                "httpOnly": False,
                "sameSite": "Lax",
            }
        )
    return out


def read_clipboard() -> str:
    if os.name != "nt":
        raise ConfigError("剪贴板导入目前只支持 Windows")
    command = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw"
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"读取剪贴板失败：{exc}") from exc
    return result.stdout.decode("utf-8", errors="replace").strip().lstrip("\ufeff")


def keep_awake() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    except Exception:  # noqa: BLE001
        pass


def allow_sleep() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    except Exception:  # noqa: BLE001
        pass


def sleep_with_log(seconds: float, logger: logging.Logger) -> None:
    end = time.time() + seconds
    while time.time() < end:
        remain = int(end - time.time())
        if remain <= 0:
            break
        logger.info(f"等待中，约 {remain // 60} 分 {remain % 60} 秒后继续（进度已保存）")
        time.sleep(min(60, remain))


def ask_with_retry(browser, store, state, cfg, logger, message, label, force_resend=False):
    waits = list(cfg["network"]["rate_limit_backoff_sec"])
    retries = max(1, int(cfg["network"].get("max_rate_limit_retries", len(waits))))
    waits = waits[:retries]
    total_wait = 0.0
    accounts_cfg = cfg.get("accounts") or {}
    accounts_enabled = bool(accounts_cfg.get("enabled")) and getattr(
        browser, "has_multiple_accounts", lambda: False
    )()
    switch_after = int(accounts_cfg.get("switch_on_rate_limit_after", -1))
    for attempt in range(len(waits) + 1):
        try:
            return browser.ask(message, label, force_resend=force_resend)
        except LoginRequired as exc:
            if accounts_enabled:
                raise SwitchAccountError(f"需要登录：{exc}") from exc
            raise StopRun(f"需要登录：{exc}。请在浏览器里登录后重新运行，进度不会丢。") from exc
        except RateLimitError as exc:
            if accounts_enabled and switch_after >= 0 and attempt >= switch_after:
                raise SwitchAccountError(f"限流：{exc}") from exc
            if attempt >= len(waits):
                raise StopRun(
                    f"连续触发限流/繁忙，已保存进度。稍后重新运行会从断点继续。最后一次提示：{exc}"
                ) from exc
            base = float(waits[attempt])
            jitter = random.uniform(0, min(30.0, base * 0.2)) if base > 0 else 0.0
            wait = base + jitter
            if total_wait + wait > float(cfg["network"]["max_total_cooldown_sec"]):
                raise StopRun(
                    "累计等待时间超过上限，已保存进度。建议过几小时再运行，脚本会自动续跑。"
                ) from exc
            store.save_state(state)
            logger.warning(f"触发限流/繁忙：{exc}；等待 {int(wait)} 秒后重试（进度已保存）")
            sleep_with_log(wait, logger)
            total_wait += wait
        except ResponseTimeout as exc:
            if accounts_enabled:
                raise SwitchAccountError(f"等待回答超时：{exc}") from exc
            browser.cancel_generation()
            if attempt >= 2:
                raise StopRun(f"连续等待回复超时，已保存进度。稍后重新运行即可续跑。") from exc
            logger.warning(f"等待回复超时：{exc}；15 秒后重试本批")
            time.sleep(15)
    raise StopRun("重试次数用尽，进度已保存")


def send_preamble(browser, store, cfg, state, logger) -> None:
    size = cfg["batch"]["size"]
    text = render(cfg["preamble"], batch_size=size, total=state.get("total_items", 0))
    logger.info("正在发送任务说明（约定字段与 JSON 格式），请稍候...")
    reply = ask_with_retry(browser, store, state, cfg, logger, text, "preamble")
    store.save_raw("preamble", 1, reply)
    state["preamble_sent"] = True
    state["conversation_url"] = browser.current_url or state.get("conversation_url", "")
    store.save_state(state)
    logger.info("任务说明已发送，开始处理名单")


def process_batch(browser, store, cfg, state, logger, items, batch_no: int, label: str) -> None:
    remaining = list(items)
    total = state.get("total_items", 0)
    max_attempts = int(cfg["batch"]["max_attempts"])
    last_error = "AI 没有返回可用结果"
    last_raw = None

    for attempt in range(1, max_attempts + 1):
        message = build_batch_message(cfg, remaining, batch_no, total, len(remaining))
        if attempt > 1:
            message += "\n\n" + cfg["retry_note"].strip()
        reply = ask_with_retry(
            browser,
            store,
            state,
            cfg,
            logger,
            message,
            label,
            force_resend=(attempt > 1),
        )
        last_raw = store.save_raw(label, attempt, reply)

        records = None
        texts = []
        for text in (reply.dom_code, reply.dom_full, reply.sse):
            if text and text.strip() and text not in texts:
                texts.append(text)
        for text in texts:
            try:
                records = extract_json_array(text)
                break
            except ParseError as exc:
                last_error = str(exc)

        if records is None:
            logger.warning(f"第 {batch_no} 批第 {attempt} 次输出无法解析：{last_error}")
            if (
                attempt == 1
                and len(remaining) > 10
                and cfg["batch"]["split_on_parse_failure"]
            ):
                half = len(remaining) // 2
                logger.info(f"把本批对半拆成 {half} + {len(remaining) - half} 条重试")
                process_batch(browser, store, cfg, state, logger, remaining[:half], batch_no, label + "a")
                process_batch(browser, store, cfg, state, logger, remaining[half:], batch_no, label + "b")
                return
            continue

        pairs, missing = match_records(remaining, records, cfg["match_field"])
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entries = []
        for item, record in pairs:
            entries.append(
                {
                    "id": item.id,
                    "input": item.value,
                    "status": "ok",
                    "record": normalize_record(record, cfg["fields"]),
                    "batch": label,
                    "attempt": attempt,
                    "conversation": browser.current_url,
                    "ts": now,
                }
            )
        store.append(entries)
        suffix = "（拆分）" if label != f"b{batch_no:04d}" else ""
        logger.info(f"第 {batch_no} 批{suffix}：保存 {len(pairs)} 条；未匹配 {len(missing)} 条")

        if len(records) != len(remaining):
            logger.warning(f"AI 返回 {len(records)} 条，本批输入 {len(remaining)} 条")

        remaining = missing
        if not remaining:
            return

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    store.append(
        [
            {
                "id": item.id,
                "input": item.value,
                "status": "failed",
                "error": last_error,
                "raw": last_raw,
                "batch": label,
                "ts": now,
            }
            for item in remaining
        ]
    )
    logger.error(f"第 {batch_no} 批有 {len(remaining)} 条重试后仍失败，已记录，可用「重试失败条目」再跑")


def export_results(store, items, cfg, logger) -> dict:
    result = store.export(
        items,
        cfg["fields"],
        cfg["output"]["json_name"],
        cfg["output"]["csv_name"],
    )
    logger.info(f"JSON 结果：{result['json']}")
    logger.info(f"CSV 结果：{result['csv']}")
    if result["missing_count"]:
        logger.info(f"未完成清单：{result['missing']}（{result['missing_count']} 条）")
    return result


def cmd_login(args) -> int:
    cfg = load_config(args.config)
    logger = setup_logger(output_dir(cfg))
    browser = DeepSeekBrowser(cfg, profile_path(cfg), logger)
    browser.start(require_login=False, prefer_login_page=True)
    print("\n浏览器已打开。请在这个 Chrome 窗口里登录 DeepSeek。")
    print("优先看右侧：如果出现「微信快捷登录」绿色按钮，直接点它，按手机微信提示确认即可，不用扫码。")
    print("如果没有快捷登录，可以：① 等 10~15 秒扫二维码；② 点底部「验证码登录」，用手机号收验证码。")
    print("重要：请用你主浏览器里同一个 DeepSeek 账号登录（通常就是那个手机号），否则看不到原来的会话。")
    print("登录后请确认左侧历史会话里有你之前建的期刊会话，确保是同一个账号。")
    print("登录成功、能看到聊天输入框后，回到这个黑色窗口按回车。")
    try:
        while True:
            answer = input("登录完成后按回车保存（输入 q 放弃）：").strip().lower()
            if answer == "q":
                break
            browser.page.wait_for_timeout(1500)
            if browser._input_locator() is not None:
                print("已检测到聊天输入框，登录成功，正在保存登录状态。")
                break
            print("还没检测到聊天输入框。请先完成登录，或关掉遮挡输入框的弹窗，然后再按回车。")
    finally:
        browser.close()
    print(f"\n登录状态已保存到 {profile_path(cfg)}，下次运行不用再登录。")
    return 0


def cmd_import_session(args) -> int:
    cfg = load_config(args.config)
    logger = setup_logger(output_dir(cfg))
    text = read_clipboard()
    local, session, cookies = parse_session_blob(text)
    cookie_count = len(_cookies_from_string(cookies)) if cookies else 0
    logger.info(
        f"剪贴板读取成功：localStorage {len(local)} 项，sessionStorage {len(session)} 项，Cookie {cookie_count} 个"
    )
    if not any("token" in key.lower() for key in list(local) + list(session)):
        logger.warning("没有看到名字里带 token 的项，导入后可能仍然是未登录状态。")

    browser = DeepSeekBrowser(cfg, profile_path(cfg), logger)
    try:
        browser.start(require_login=False)
        page = browser.page
        page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded")
        page.evaluate(
            """(data) => {
              for (const [k, v] of Object.entries(data.local || {})) localStorage.setItem(k, v);
              for (const [k, v] of Object.entries(data.session || {})) sessionStorage.setItem(k, v);
            }""",
            {"local": local, "session": session},
        )
        if cookies:
            browser.context.add_cookies(_cookies_from_string(cookies))
        page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded")

        deadline = time.time() + 45
        ok = False
        while time.time() < deadline:
            if browser._input_locator() is not None:
                ok = True
                break
            page.wait_for_timeout(1000)

        if ok:
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value ' '"],
                    capture_output=True,
                    timeout=10,
                )
            except Exception:  # noqa: BLE001
                pass
            logger.info("登录状态导入成功，已保存到浏览器配置目录（剪贴板已清空）。")
            return 0

        png = output_dir(cfg) / "import_session.png"
        try:
            page.screenshot(path=str(png))
        except Exception:  # noqa: BLE001
            pass
        logger.error("导入后仍然没有进入聊天页。常见原因：token 不完整/已过期，或复制错了键。")
        logger.error(f"页面截图：{png}")
        return 2
    finally:
        browser.close()


def cmd_status(args) -> int:
    cfg = load_config(args.config)
    items, meta = load_items(cfg)
    outdir = output_dir(cfg)
    store = Store(outdir, cfg)
    state = store.load_state()
    counts = store.counts(items)
    progress = counts["ok"] / counts["total"] * 100 if counts["total"] else 0
    print(f"\n输入文件：{meta['path']}")
    print(f"总数：{counts['total']}    重复名称：{meta['duplicates']}")
    print(f"已完成：{counts['ok']}    失败：{counts['failed']}    待处理：{counts['pending']}    ({progress:.1f}%)")
    if state.get("conversation_url"):
        print(f"当前会话：{state['conversation_url']}")
    if state.get("updated_at"):
        print(f"最后更新：{state['updated_at']}")
    print(f"结果目录：{outdir}")
    return 0


def cmd_export(args) -> int:
    cfg = load_config(args.config)
    items, _ = load_items(cfg)
    logger = setup_logger(output_dir(cfg))
    store = Store(output_dir(cfg), cfg)
    include_missing = bool(getattr(args, "include_missing", False))
    result = store.export(
        items,
        cfg["fields"],
        cfg["output"]["json_name"],
        cfg["output"]["csv_name"],
        include_missing=include_missing,
    )
    logger.info(f"JSON：{result['json']}（{result['ok']} 条）")
    logger.info(f"CSV：{result['csv']}")
    logger.info(f"未完成清单：{result['missing']}（{result['missing_count']} 条）")
    return 0


def cmd_retry_failed(args) -> int:
    cfg = load_config(args.config)
    items, _ = load_items(cfg)
    store = Store(output_dir(cfg), cfg)
    failed = [item_id for item_id, entry in store.last_entries().items() if entry.get("status") == "failed"]
    if not failed:
        print("没有失败的条目。")
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    store.append([{"id": item_id, "status": "retry", "ts": now} for item_id in failed])
    print(f"已把 {len(failed)} 条失败记录标记为待重试，下次运行会重新处理。")
    return 0


def cmd_doctor(args) -> int:
    cfg = load_config(args.config)
    logger = setup_logger(output_dir(cfg))
    browser = DeepSeekBrowser(cfg, profile_path(cfg), logger)
    png = output_dir(cfg) / "doctor.png"
    try:
        browser.start(require_login=False)
        browser.page.wait_for_timeout(5000)
        if browser._on_login_page() or browser._input_locator() is None:
            print("\n当前还没有登录（或聊天页没有加载出来）。请先用菜单 1 完成登录。")
            browser.page.screenshot(path=str(png))
            print(f"页面截图：{png}")
            return 2
        info = browser.page.evaluate(
            """
            () => {
              const inputs = [...document.querySelectorAll('textarea, div[contenteditable="true"]')].map(el => {
                const r = el.getBoundingClientRect();
                return {tag: el.tagName, placeholder: el.getAttribute('placeholder') || '',
                        visible: r.width > 0 && r.height > 0,
                        width: Math.round(r.width), height: Math.round(r.height), bottom: Math.round(r.bottom)};
              });
              const labels = [...new Set([...document.querySelectorAll('[aria-label]')]
                .map(e => e.getAttribute('aria-label')).filter(Boolean))].slice(0, 60);
              return {url: location.href, title: document.title,
                      markdown_count: document.querySelectorAll('.ds-markdown').length,
                      inputs: inputs, aria_labels: labels,
                      body_tail: document.body.innerText.slice(-400)};
            }
            """
        )
        print("\n========== 页面检查结果 ==========")
        print(json.dumps(info, ensure_ascii=False, indent=2))
        print("\n输入框定位：OK")
        print("会话 URL：", browser.current_url or "（当前是新会话，还没有会话链接）")
        browser.page.screenshot(path=str(png))
        print(f"页面截图：{png}")
        return 0
    finally:
        browser.close()


def cmd_preview(args) -> int:
    cfg = load_config(args.config)
    if args.batch_size:
        cfg["batch"]["size"] = int(args.batch_size)
    items, meta = load_items(cfg)
    total = len(items)
    preamble = render(cfg["preamble"], batch_size=cfg["batch"]["size"], total=total)
    print("\n========== 任务说明（新会话时发送一次） ==========\n")
    print(preamble)
    print("\n========== 第 1 批（示例） ==========\n")
    print(build_batch_message(cfg, items[: cfg["batch"]["size"]], 1, total, total))
    print(f"\n共 {total} 条，每批 {cfg['batch']['size']} 条，约 {-(-total // cfg['batch']['size'])} 批。")
    if cfg["deepseek"]["conversation_url"]:
        print(f"已配置会话链接，运行时将直接进入：{cfg['deepseek']['conversation_url']}")
    return 0


def _switch_to_next_account(browser, store, cfg, state, logger, reason) -> bool:
    accounts_cfg = cfg.get("accounts") or {}
    if not accounts_cfg.get("enabled") or not getattr(
        browser, "has_multiple_accounts", lambda: False
    )():
        return False
    logger.warning(f"{reason}；准备切换账号...")
    for _ in range(len(browser.accounts)):
        try:
            switched = browser.switch_account()
        except LoginRequired as exc:
            logger.warning(f"切换账号需要登录：{exc}")
            switched = False
        if switched:
            state["conversation_url"] = ""
            state["preamble_sent"] = False
            state["batches_in_conversation"] = 0
            state["active_account"] = browser.current_account_name()
            store.save_state(state)
            browser.new_chat()
            logger.warning(
                f"已切换到账号「{state['active_account']}」，新开会话并重发任务说明，进度继续"
            )
            return True
        wait = browser.seconds_until_account() if hasattr(browser, "seconds_until_account") else 0
        if wait <= 0:
            return False
        logger.warning(f"所有账号都在恢复期，等待 {int(wait)} 秒后再试")
        sleep_with_log(min(wait, 1800), logger)
    return False


def _run_loop(browser, store, cfg, state, logger, pending, items, force_new: bool = False) -> None:
    target = "" if force_new else (state.get("conversation_url") or cfg["deepseek"]["conversation_url"])
    started_new = False

    if target:
        try:
            logger.info(f"打开已有会话：{target}")
            browser.goto_conversation(target)
            state["conversation_url"] = target
            store.save_state(state)
        except BrowserError as exc:
            logger.warning(f"已有会话打不开（{exc}），改为新建会话")
            browser.new_chat()
            started_new = True
    else:
        browser.new_chat()
        started_new = True

    state["active_account"] = browser.current_account_name()
    store.save_state(state)

    need_preamble = False
    if started_new:
        need_preamble = cfg["deepseek"]["send_preamble"] != "never"
    elif cfg["deepseek"]["send_preamble"] == "always":
        need_preamble = True
    elif not state.get("preamble_sent"):
        state["preamble_sent"] = True
        store.save_state(state)

    if need_preamble:
        try:
            send_preamble(browser, store, cfg, state, logger)
        except (SwitchAccountError, StuckError) as exc:
            if _switch_to_next_account(browser, store, cfg, state, logger, str(exc)):
                pending = [item for item in pending if not store.is_done(item.id)]
            else:
                raise StopRun(f"所有账号都不可用，已保存进度：{exc}") from exc
    elif cfg["deepseek"]["send_preamble"] == "never":
        logger.warning("send_preamble=never：不会发送任务说明，请确认已手工交代过格式")
        state["preamble_sent"] = True
        store.save_state(state)

    size = cfg["batch"]["size"]
    rotate_every = int(cfg["batch"]["rotate_every_batches"])

    while pending:
        if rotate_every > 0 and int(state.get("batches_in_conversation", 0)) >= rotate_every:
            logger.info(f"当前会话已发 {rotate_every} 批，新开会话避免上下文超长")
            try:
                browser.new_chat()
            except BrowserError as exc:
                if _switch_to_next_account(browser, store, cfg, state, logger, f"新建会话失败：{exc}"):
                    pending = [item for item in pending if not store.is_done(item.id)]
                    continue
                raise
            state["batches_in_conversation"] = 0
            state["preamble_sent"] = False
            state["conversation_url"] = ""
            store.save_state(state)

        if not state.get("preamble_sent"):
            if cfg["deepseek"]["send_preamble"] != "never":
                try:
                    send_preamble(browser, store, cfg, state, logger)
                except (SwitchAccountError, StuckError) as exc:
                    if _switch_to_next_account(browser, store, cfg, state, logger, str(exc)):
                        pending = [item for item in pending if not store.is_done(item.id)]
                        continue
                    raise StopRun(f"所有账号都不可用，已保存进度：{exc}") from exc
            else:
                logger.warning("新会话没有任务说明（send_preamble=never），结果可能不符合格式")
                state["preamble_sent"] = True
                state["conversation_url"] = browser.current_url or state.get("conversation_url", "")
                store.save_state(state)

        batch_no = int(state.get("batch_counter", 0)) + 1
        state["batch_counter"] = batch_no
        batch = pending[:size]
        logger.info(f"开始第 {batch_no} 批，共 {len(batch)} 条（待处理 {len(pending)} 条）")

        try:
            process_batch(browser, store, cfg, state, logger, batch, batch_no, f"b{batch_no:04d}")
        except (SwitchAccountError, StuckError) as exc:
            if _switch_to_next_account(browser, store, cfg, state, logger, str(exc)):
                pending = [item for item in pending if not store.is_done(item.id)]
                continue
            raise StopRun(f"所有账号都卡住或需要登录，已保存进度：{exc}") from exc

        current = browser.current_url
        previous = state.get("conversation_url", "")
        if current and previous and current != previous:
            logger.warning("网页自动切换了会话（可能是上下文超长自动新建），下一批前会重新发送任务说明")
            state["preamble_sent"] = False
        state["conversation_url"] = current or previous
        state["batches_in_conversation"] = int(state.get("batches_in_conversation", 0)) + 1
        store.save_state(state)

        pending = [item for item in pending if not store.is_done(item.id)]
        counts = store.counts(items)
        logger.info(
            f"进度：{counts['ok']} / {counts['total']}（失败 {counts['failed']}，待处理 {counts['pending']}）"
        )

        if pending:
            low, high = cfg["batch"]["cooldown_sec"]
            wait = random.uniform(float(low), float(high))
            logger.info(f"本批结束，休息 {int(wait)} 秒")
            time.sleep(wait)


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    if args.batch_size:
        cfg["batch"]["size"] = int(args.batch_size)
    items, meta = load_items(cfg)
    outdir = output_dir(cfg)
    logger = setup_logger(outdir)
    store = Store(outdir, cfg)

    if args.new_run:
        archived = store.archive_existing()
        if archived:
            logger.info(f"旧结果已归档到：{archived}")
            store = Store(outdir, cfg)

    state = store.load_state()
    if state.get("input_fingerprint") and state["input_fingerprint"] != meta["fingerprint"]:
        raise ConfigError(
            "输入文件内容和上次运行不一致。为避免两批数据混在一起，已停止。\n"
            "如果确实要开始新任务，请运行：python -m src.main run --new-run"
        )
    if not state:
        state = {"started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    state["input_fingerprint"] = meta["fingerprint"]
    state["input_file"] = meta["path"]
    state["total_items"] = meta["total"]
    state.setdefault("batch_counter", 0)
    state.setdefault("batches_in_conversation", 0)
    state.setdefault("preamble_sent", False)
    state.setdefault("conversation_url", "")
    store.save_state(state)

    counts = store.counts(items)
    logger.info(f"输入 {meta['total']} 条，每批 {cfg['batch']['size']} 条")
    logger.info(f"当前进度：成功 {counts['ok']}，失败 {counts['failed']}，待处理 {counts['pending']}")
    if meta["duplicates"]:
        logger.warning(f"名单中有 {meta['duplicates']} 个重复名称，会按原表逐条处理")

    pending = [item for item in items if not store.is_done(item.id)]
    if args.limit:
        pending = pending[: int(args.limit)]
        logger.info(f"本次只处理前 {len(pending)} 条（测试模式）")

    if not pending:
        logger.info("所有条目都已处理完。")
        export_results(store, items, cfg, logger)
        return 0

    if args.dry_run:
        print("\n========== 任务说明 ==========\n")
        print(render(cfg["preamble"], batch_size=cfg["batch"]["size"], total=meta["total"]))
        print("\n========== 第 1 批 ==========\n")
        print(build_batch_message(cfg, pending[: cfg["batch"]["size"]], 1, meta["total"], len(pending)))
        print("\n（dry-run：未启动浏览器、未发送任何内容）")
        return 0

    browser = DeepSeekBrowser(cfg, profile_path(cfg), logger)
    browser.use_account(state.get("active_account", ""))
    keep_awake()
    exit_code = 0
    try:
        logger.info("正在启动浏览器...")
        browser.start()
        _run_loop(browser, store, cfg, state, logger, pending, items, force_new=args.fresh_chat)
        logger.info("本次运行完成，所有待处理条目已跑完。")
    except StopRun as exc:
        logger.error(str(exc))
        exit_code = 2
    except LoginRequired as exc:
        logger.error(f"需要登录：{exc}")
        exit_code = 2
    except KeyboardInterrupt:
        logger.warning("收到中断，正在保存进度...")
        exit_code = 130
    except (BrowserError, PWError) as exc:
        logger.error(f"浏览器出错：{exc}。进度已保存，重新运行会续跑。")
        exit_code = 3
    finally:
        browser.close()
        allow_sleep()
        try:
            export_results(store, items, cfg, logger)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"导出结果失败：{exc}")

    counts = store.counts(items)
    logger.info(f"最终进度：成功 {counts['ok']} / {counts['total']}，失败 {counts['failed']}，待处理 {counts['pending']}")
    return exit_code


def menu() -> int:
    base = SimpleNamespace(
        config="config.yaml",
        limit=None,
        batch_size=None,
        new_run=False,
        dry_run=False,
        fresh_chat=False,
        include_missing=False,
    )
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        print("=" * 50)
        print("        DeepSeek 网页批量处理工具")
        print("=" * 50)
        print("  1. 首次登录 DeepSeek（只需一次）")
        print("  2. 试跑 3 条（先看效果）")
        print("  3. 正式运行（自动从断点继续）")
        print("  4. 查看进度")
        print("  5. 导出 JSON / CSV")
        print("  6. 预览将发送的内容（不联网）")
        print("  7. 重试失败的条目")
        print("  8. 全新开始（归档旧结果）")
        print("  9. 检查页面结构（登录后建议先跑）")
        print(" 10. 导入已登录浏览器的 token（登录页打不开时）")
        print(" 11. 打开网页控制台（推荐）")
        print("  0. 退出")
        print("=" * 50)
        choice = input("请输入序号：").strip()
        if choice == "0":
            return 0
        try:
            if choice == "1":
                cmd_login(base)
            elif choice == "2":
                args = SimpleNamespace(**vars(base))
                args.limit = 3
                cmd_run(args)
            elif choice == "3":
                cmd_run(SimpleNamespace(**vars(base)))
            elif choice == "4":
                cmd_status(base)
            elif choice == "5":
                cmd_export(base)
            elif choice == "6":
                cmd_preview(base)
            elif choice == "7":
                cmd_retry_failed(base)
            elif choice == "8":
                confirm = input("会归档旧结果并从头开始，输入 YES 继续：").strip().upper()
                if confirm == "YES":
                    args = SimpleNamespace(**vars(base))
                    args.new_run = True
                    cmd_run(args)
            elif choice == "9":
                cmd_doctor(base)
            elif choice == "10":
                cmd_import_session(base)
            elif choice == "11":
                root = Path(__file__).resolve().parent.parent
                subprocess.Popen(
                    ["cmd", "/c", "start", "", str(root / "start_ui.bat")],
                    cwd=str(root),
                )
                print("已在新窗口启动网页控制台，稍等片刻会自动打开浏览器。")
            else:
                print("没有这个序号。")
        except (ConfigError, InputError, BrowserError) as exc:
            print(f"\n[错误] {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"\n[出错] {exc}")
        input("\n按回车返回菜单...")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DeepSeek 网页批量处理工具")
    default_config = Path(__file__).resolve().parent.parent / "config.yaml"
    parser.add_argument("--config", default=str(default_config), help="配置文件路径")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("menu", help="打开菜单")
    sub.add_parser("login", help="首次登录 DeepSeek")
    sub.add_parser("doctor", help="检查登录后的页面结构")
    sub.add_parser("import-session", help="从剪贴板导入已登录浏览器的 token")

    run = sub.add_parser("run", help="开始/继续跑任务")
    run.add_argument("--limit", type=int, help="本次最多处理多少条（测试用）")
    run.add_argument("--batch-size", type=int, help="临时覆盖每批条数")
    run.add_argument("--new-run", action="store_true", help="归档旧结果，从头开始")
    run.add_argument("--dry-run", action="store_true", help="只预览，不启动浏览器")
    run.add_argument("--fresh-chat", action="store_true", help="强制新建会话")

    sub.add_parser("status", help="查看进度")
    sub.add_parser("retry-failed", help="把失败条目标记为待重试")

    export = sub.add_parser("export", help="导出 JSON/CSV")
    export.add_argument("--include-missing", action="store_true", help="把未完成条目也写进 JSON")

    preview = sub.add_parser("preview", help="预览任务说明和第一批")
    preview.add_argument("--batch-size", type=int, help="临时覆盖每批条数")
    return parser


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "menu"
    args.config = str(Path(args.config).resolve())
    try:
        if command == "menu":
            return menu()
        if command == "login":
            return cmd_login(args)
        if command == "doctor":
            return cmd_doctor(args)
        if command == "import-session":
            return cmd_import_session(args)
        if command == "run":
            return cmd_run(args)
        if command == "status":
            return cmd_status(args)
        if command == "export":
            return cmd_export(args)
        if command == "retry-failed":
            return cmd_retry_failed(args)
        if command == "preview":
            return cmd_preview(args)
        parser.print_help()
        return 1
    except (ConfigError, InputError, BrowserError) as exc:
        print(f"\n[错误] {exc}")
        return 1
    except KeyboardInterrupt:
        print("\n已中断。进度已保存，下次运行会从断点继续。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
