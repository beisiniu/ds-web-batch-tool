from __future__ import annotations

import csv
import json
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import yaml
from flask import Flask, Response, jsonify, request, send_file
from openpyxl import load_workbook
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from .config import ConfigError, load_config
from .inputs import InputError, _looks_like_xlsx, load_items

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "tasks"
UPLOADS = TASKS / "_uploads"
SESSION_DIR = TASKS / "_session"
PROFILE_DIR = ROOT / "browser-profile"
ACCOUNT_STATUS = TASKS / "_accounts" / "status.json"
STATIC = Path(__file__).resolve().parent / "web_static"

APP_TOKEN = secrets.token_urlsafe(24)
JOB = {"task_id": None, "proc": None, "stopped": False, "log_file": None}
LOGIN_JOB = {"name": "", "proc": None}
COUNTS_CACHE: dict = {}
UPLOAD_RE = re.compile(r"^[0-9a-f]{16}$")
TASK_RE = re.compile(r"^[0-9A-Za-z_-]{5,40}$")
FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")

BATCH_HEADER = (
    "下面是第 {batch_no} 批，共 {count} 个（整个任务共 {total} 个）。"
    "请严格按照约定输出 JSON 数组，不要解释，顺序与下列名称一致："
)
RETRY_NOTE = (
    "注意：你上一次的输出无法被解析为完整 JSON 数组。请重新输出本批全部结果，"
    "只输出 JSON 数组本身（可以放在 ```json 代码块中），不要任何解释。"
)

JOURNAL_TEMPLATE = """你是一名严谨的期刊信息核查助手。下面是一批期刊名称（每批约 {batch_size} 个），请逐本核查并整理元数据。

核查重点：
- 优先确认期刊官方网站、主办/主管单位官网；没有独立官网时，使用知网、万方、维普等主要收录或采编平台
- 同名不同刊、更名、不同版本（自然科学版/社会科学版/教育科研版等）必须在备注字段中写明区分依据
- 以可验证的公开来源为准，不要根据名称猜测 URL

{extra_notes}请对每本期刊输出以下字段：
{fields}"""

SCHOLAR_TEMPLATE = """你是一名严谨的学术信息核查助手。下面是一批学者姓名（每批约 {batch_size} 位），请逐人核查公开学术信息。

核查重点：
- 优先个人主页、机构师资页；没有时使用 Google Scholar、ORCID、ResearchGate 等学术档案页
- 同名学者必须结合任职机构、研究方向等区分，并在备注字段写明判断依据
- 不要编造职称、论文、奖项、邮箱或主页

{extra_notes}请对每位学者输出以下字段：
{fields}"""

INSTITUTION_TEMPLATE = """你是一名严谨的中国机构信息核查助手。下面是一批机构名称（每批约 {batch_size} 个），请逐个联网核查公开信息，整理成机构档案。

核查与判定规则：
- 先用机构全称确认唯一主体，结合省份、城市、机构类型和历史沿革区分同名或近名机构；机构名称必须原样保留，不要改变主体
- 官网优先机构一级域名或举办单位官网；官网打不开时可参考事业单位登记信息、教育/卫健/民政/科技等主管部门官网、机构官方公众号
- 严禁按名称猜测：官网、成立时间、地址、电话、邮箱、负责人、隶属关系都须有公开来源；无法确认的字段一律填空字符串
- 电话、邮箱、邮编必须逐字核对官网「联系我们」等官方页面；不能确认就留空，不要推测或套用相近号码
- 英文名优先使用官网英文版或官方英文资料中的写法；没有官方英文名时给通用规范译名，不要生造缩写；荣誉尽量列全并保留官方原名称
- 研究领域、学科方向或主要业务尽量列全，不要只写概括类别
- 成立日期优先采用批准/批复设立的日期，不要用挂牌或成立大会日期替代
- 下属机构只填具有独立建制的单位；内设部门、科室、办公室不算下属机构
- 已撤销、合并或更名的机构：正式更名前使用过的名称写入 name_used（前身/起源机构不算曾用名，可填入 institution_related），变更时间写入 change_date，撤销/停办时间写入 death_date，承接机构写入 is_replaced_by
- 简介（intro）100~300 字，概括机构性质、隶属、历史沿革、规模与主要职能；不要大段复制原文或写宣传语
- 多值字段用英文分号加空格「; 」分隔；日期只能写 YYYY 或 YYYY/M/D；不要输出“不详”“未知”，统一留空
- 所有字段值都输出字符串；数字、邮编、日期同样用字符串
- 输出中不要出现任何引用标记（如 [reference:0]、[citation:1]）、脚注、网址列表或 markdown 链接

{extra_notes}请对每个机构输出以下字段：
{fields}"""

DOMAIN_TEMPLATES = {
    "journal": JOURNAL_TEMPLATE,
    "scholar": SCHOLAR_TEMPLATE,
    "institution": INSTITUTION_TEMPLATE,
}

CUSTOM_FIELDS_HEADER = "请严格按以下字段输出（字段名必须完全一致）："

app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def _task_dir(task_id: str) -> Path:
    return TASKS / task_id


def _config_path(task_id: str) -> Path:
    return _task_dir(task_id) / "config.yaml"


def _api_error(message: str, code: int = 400):
    return jsonify({"error": message}), code


def _fields_block(fields: list[dict]) -> str:
    lines = []
    for index, field in enumerate(fields, 1):
        key = str(field.get("key", "")).strip()
        if not key:
            continue
        desc = str(field.get("desc", "")).strip()
        lines.append(f"{index}. {key}" + (f"：{desc}" if desc else ""))
    return "\n".join(lines) or "（尚未配置字段）"


def _format_rules(name_field: str) -> str:
    name_line = (
        f"- 用于对齐的字段「{name_field}」必须原样保留输入的名称，不要翻译或改写\n"
        if name_field
        else ""
    )
    return (
        "输出格式（必须遵守）：\n"
        "- 只输出一个 JSON 数组，不要输出解释、前言、总结\n"
        "- 数组元素数量必须与输入名称数量完全一致，顺序一致\n"
        "- 每个元素必须包含且只包含上述字段，字段名用英文双引号\n"
        f"{name_line}"
        '- 无法确认的信息用空字符串 ""，绝对不要编造 URL、数字或事实\n'
        "- 可以把 JSON 放在 ```json 代码块中"
    )


def build_preamble(
    domain: str,
    fields: list[dict],
    match_field: str = "",
    notes: str = "",
    custom_prompt: str = "",
) -> str:
    fields_text = _fields_block(fields)
    first_key = next(
        (str(field.get("key", "")).strip() for field in fields if str(field.get("key", "")).strip()),
        "",
    )
    name_field = match_field or first_key
    rules = _format_rules(name_field)
    domain = domain if domain in ("journal", "scholar", "institution", "custom") else "journal"
    if domain == "custom":
        head = (custom_prompt or "").strip() or "（请先填写任务提示词，下面是要输出的字段和格式规则）"
        return f"{head}\n\n{CUSTOM_FIELDS_HEADER}\n{fields_text}\n\n{rules}"
    extra = f"补充要求：\n{notes.strip()}\n\n" if notes.strip() else ""
    text = DOMAIN_TEMPLATES[domain].replace("{extra_notes}", extra).replace("{fields}", fields_text)
    confirm = "收到本说明后请只回复「明白」，不要输出示例数据，等待我发送第一批名称。"
    return f"{text.rstrip()}\n\n{rules}\n\n{confirm}"


def _cell(value) -> str:
    if value is None:
        return ""
    return str(value).replace("\u200b", "").strip()


def _detect_encoding(path: Path) -> str:
    raw = path.read_bytes()[:200000]
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            raw.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "utf-8"


def _read_table(path: Path, sheet=0, limit: int = 12):
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm") or (suffix in (".csv", ".txt") and _looks_like_xlsx(path)):
        with open(path, "rb") as fh:
            wb = load_workbook(fh, read_only=True, data_only=True)
            try:
                sheets = wb.sheetnames
                if isinstance(sheet, str) and not sheet.isdigit():
                    ws = wb[sheet]
                else:
                    ws = wb.worksheets[int(sheet)]
                rows = []
                for index, row in enumerate(ws.iter_rows(values_only=True)):
                    if index >= limit:
                        break
                    rows.append([_cell(value) for value in row])
                return sheets, rows
            finally:
                wb.close()
    if suffix in (".csv", ".txt"):
        encoding = _detect_encoding(path)
        with open(path, "r", encoding=encoding, newline="") as f:
            reader = csv.reader(f)
            rows = []
            for index, row in enumerate(reader):
                if index >= limit:
                    break
                rows.append([_cell(value) for value in row])
        return [], rows
    raise ValueError(f"不支持的文件类型：{suffix}")


def _preview_payload(path: Path, sheet=0, has_header: bool = True, limit: int = 8) -> dict:
    sheets, raw = _read_table(path, sheet, limit=12)
    if not raw:
        return {"sheets": sheets, "headers": [], "rows": [], "columns": 0}
    width = min(max(len(row) for row in raw), 40)
    for row in raw:
        if len(row) < width:
            row.extend([""] * (width - len(row)))
        elif len(row) > width:
            del row[width:]
    if has_header:
        headers = [cell if cell else f"第{i + 1}列" for i, cell in enumerate(raw[0])]
        rows = raw[1 : 1 + limit]
    else:
        headers = [f"第{i + 1}列" for i in range(width)]
        rows = raw[:limit]
    return {"sheets": sheets, "headers": headers, "rows": rows, "columns": width}


def _counts(task: dict) -> dict:
    total = int(task.get("total") or 0)
    path = _task_dir(task["id"]) / "output" / "records.jsonl"
    if not path.exists():
        return {"ok": 0, "failed": 0, "pending": total, "total": total}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0
    key = (str(path), mtime, total)
    cached = COUNTS_CACHE.get(key)
    if cached:
        return cached
    last: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("id"):
                    last[entry["id"]] = entry.get("status", "")
    except OSError:
        pass
    ok = sum(1 for status in last.values() if status == "ok")
    failed = sum(1 for status in last.values() if status == "failed")
    result = {"ok": ok, "failed": failed, "pending": max(0, total - ok - failed), "total": total}
    for old_key in [k for k in COUNTS_CACHE if k[0] == str(path) and k != key]:
        COUNTS_CACHE.pop(old_key, None)
    COUNTS_CACHE[key] = result
    return result


def _job_state(task_id: str) -> str:
    proc = JOB.get("proc")
    if JOB.get("task_id") != task_id or proc is None:
        return "idle"
    code = proc.poll()
    if code is None:
        return "running"
    if code == 0:
        return "completed"
    return "stopped" if JOB.get("stopped") else "failed"


def _public_task(task: dict, full: bool = False) -> dict:
    counts = _counts(task)
    job = _job_state(task["id"])
    if job == "idle":
        if counts["total"] and counts["pending"] == 0:
            job = "completed"
        elif counts["ok"] or counts["failed"]:
            job = "stopped"
        else:
            job = "pending"
    data = {
        "id": task["id"],
        "name": task.get("name", ""),
        "created_at": task.get("created_at", ""),
        "total": counts["total"],
        "duplicates": task.get("duplicates", 0),
        "batch_size": task.get("batch_size", 50),
        "fields_count": len(task.get("fields") or []),
        "counts": counts,
        "job": job,
    }
    if full:
        data.update(
            {
                "fields": task.get("fields", []),
                "preamble": task.get("preamble", ""),
                "match_field": task.get("match_field", ""),
                "column": task.get("column"),
                "has_header": task.get("has_header"),
                "input_name": task.get("input_name", ""),
                "conversation_url": task.get("conversation_url", ""),
            }
        )
    return data


def _tail_log(task_id: str, lines: int = 100) -> str:
    for name in ("run.log", "stdout.log"):
        path = _task_dir(task_id) / "output" / name
        if path.exists():
            try:
                data = path.read_bytes()[-64 * 1024 :]
            except OSError:
                return ""
            return "\n".join(data.decode("utf-8", errors="replace").splitlines()[-lines:])
    return ""


def _load_task(task_id: str) -> dict | None:
    if not TASK_RE.fullmatch(task_id):
        return None
    task = _read_json(_task_dir(task_id) / "task.json")
    return task if isinstance(task, dict) else None


def _load_accounts() -> dict:
    path = ROOT / "accounts.yaml"
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    accounts = data.get("accounts") or {}
    profiles = []
    for index, item in enumerate(accounts.get("profiles") or []):
        profile = Path(str(item.get("profile_dir") or ""))
        if not profile.is_absolute():
            profile = ROOT / profile
        profiles.append(
            {"name": str(item.get("name") or f"账号{index + 1}"), "profile_dir": str(profile)}
        )
    result = dict(accounts)
    result["profiles"] = profiles
    return result


def _save_account_record(name: str, profile_dir: Path) -> None:
    path = ROOT / "accounts.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    data = data or {}
    accounts = data.setdefault("accounts", {})
    accounts.setdefault("enabled", True)
    accounts.setdefault("stuck_switch_sec", 300)
    accounts.setdefault("account_cooldown_sec", 600)
    accounts.setdefault("switch_on_rate_limit_after", 0)
    profiles = accounts.setdefault("profiles", [])
    target = str(Path(profile_dir).resolve()).lower()
    for item in profiles:
        if str(Path(str(item.get("profile_dir") or "")).resolve()).lower() == target:
            item["name"] = name
            break
    else:
        profiles.append({"name": name, "profile_dir": str(profile_dir)})
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _account_slug(name: str, index: int) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "", name)
    return f"{index:02d}-{safe or 'acct'}"


def _account_check_config(profile_dir: Path, slug: str) -> Path:
    folder = TASKS / "_accounts" / slug
    folder.mkdir(parents=True, exist_ok=True)
    dummy = folder / "dummy.csv"
    if not dummy.exists():
        dummy.write_text("name\n示例\n", encoding="utf-8")
    config = {
        "input": {"file": str(dummy), "sheet": 0, "column": "name", "has_header": True},
        "output": {
            "dir": str(folder / "output"),
            "json_name": "result.json",
            "csv_name": "result.csv",
        },
        "browser": {
            "channel": "chrome",
            "profile_dir": str(profile_dir),
            "no_proxy": True,
            "close_stale": True,
            "headless": True,
        },
        "batch": {
            "size": 1,
            "rotate_every_batches": 0,
            "max_attempts": 1,
            "split_on_parse_failure": False,
            "cooldown_sec": [0, 0],
        },
        "deepseek": {
            "conversation_url": "",
            "send_preamble": "never",
            "response_timeout_sec": 60,
            "login_wait_sec": 60,
            "verification_wait_sec": 60,
        },
        "network": {
            "rate_limit_backoff_sec": [30],
            "max_rate_limit_retries": 1,
            "max_total_cooldown_sec": 120,
        },
        "fields": ["name"],
        "match_field": "name",
        "preamble": "账号登录状态检查，不会发送任何消息。",
        "batch_header": BATCH_HEADER,
        "retry_note": RETRY_NOTE,
    }
    path = folder / "check.yaml"
    _write_yaml(path, config)
    return path


def _account_login_config(profile_dir: Path, slug: str) -> Path:
    folder = TASKS / "_accounts" / slug
    folder.mkdir(parents=True, exist_ok=True)
    dummy = folder / "dummy.csv"
    if not dummy.exists():
        dummy.write_text("name\n示例\n", encoding="utf-8")
    config = {
        "input": {"file": str(dummy), "sheet": 0, "column": "name", "has_header": True},
        "output": {
            "dir": str(folder / "output"),
            "json_name": "result.json",
            "csv_name": "result.csv",
        },
        "browser": {
            "channel": "chrome",
            "profile_dir": str(profile_dir),
            "no_proxy": True,
            "close_stale": True,
            "headless": False,
        },
        "batch": {
            "size": 1,
            "rotate_every_batches": 0,
            "max_attempts": 1,
            "split_on_parse_failure": False,
            "cooldown_sec": [0, 0],
        },
        "deepseek": {
            "conversation_url": "",
            "send_preamble": "never",
            "response_timeout_sec": 60,
            "login_wait_sec": 600,
            "verification_wait_sec": 600,
        },
        "network": {
            "rate_limit_backoff_sec": [30],
            "max_rate_limit_retries": 1,
            "max_total_cooldown_sec": 120,
        },
        "fields": ["name"],
        "match_field": "name",
        "preamble": "账号直接登录，不会发送任何消息。",
        "batch_header": BATCH_HEADER,
        "retry_note": RETRY_NOTE,
    }
    path = folder / "login.yaml"
    _write_yaml(path, config)
    return path


def _run_account_check(name: str, profile_dir: Path, slug: str) -> dict:
    checked_at = _now()
    ok = False
    url = ""
    error = ""
    try:
        config_path = _account_check_config(profile_dir, slug)
        result = subprocess.run(
            [sys.executable, "-m", "src.main", "--config", str(config_path), "check-login"],
            cwd=str(ROOT),
            capture_output=True,
            timeout=90,
        )
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        marker = "__CHECK_RESULT__"
        for line in output.splitlines():
            if line.startswith(marker):
                data = json.loads(line[len(marker) :])
                ok = bool(data.get("ok"))
                url = str(data.get("url") or "")
                error = str(data.get("error") or "")
        if not ok and not error:
            error = "登录已失效（页面停在登录页）" if "/sign_in" in url else output[-1200:]
    except subprocess.TimeoutExpired:
        error = "检查超时"
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    return {
        "name": name,
        "profile_dir": str(profile_dir),
        "ok": ok,
        "url": url,
        "error": error,
        "checked_at": checked_at,
    }


def _status_key(profile_dir: str | Path) -> str:
    return str(Path(profile_dir).resolve()).lower()


def _load_account_status() -> dict:
    data = _read_json(ACCOUNT_STATUS, {}) or {}
    items = data.get("items")
    return items if isinstance(items, dict) else {}


def _save_account_status(results: list[dict], replace: bool = False) -> None:
    items = {} if replace else _load_account_status()
    for item in results:
        items[_status_key(item["profile_dir"])] = {
            "ok": bool(item["ok"]),
            "url": item.get("url", ""),
            "error": item.get("error", ""),
            "checked_at": item.get("checked_at", _now()),
        }
    _write_json(ACCOUNT_STATUS, {"checked_at": _now(), "items": items})


def _accounts_payload(accounts: dict | None = None) -> list[dict]:
    accounts = accounts if accounts is not None else _load_accounts()
    status = _load_account_status()
    out = []
    for item in accounts.get("profiles") or []:
        entry = status.get(_status_key(item["profile_dir"])) or {}
        out.append(
            {
                "name": item["name"],
                "profile_dir": item["profile_dir"],
                "ok": entry.get("ok"),
                "checked_at": entry.get("checked_at", ""),
                "error": entry.get("error", ""),
            }
        )
    return out


def _build_config(task: dict) -> dict:
    return {
        "_runtime_path": str(_task_dir(task["id"]) / "runtime.json"),
        "input": {
            "file": task["input_file"],
            "sheet": task.get("sheet", 0),
            "column": task.get("column", 1),
            "has_header": task.get("has_header", True),
        },
        "output": {
            "dir": str(_task_dir(task["id"]) / "output"),
            "json_name": "result.json",
            "csv_name": "result.csv",
        },
        "browser": {
            "channel": "chrome",
            "profile_dir": str(PROFILE_DIR),
            "no_proxy": True,
            "close_stale": True,
        },
        "accounts": _load_accounts(),
        "batch": {
            "size": int(task.get("batch_size", 50)),
            "rotate_every_batches": int(task.get("rotate_every_batches", 8)),
            "max_attempts": 3,
            "split_on_parse_failure": True,
            "cooldown_sec": task.get("cooldown_sec", [8, 25]),
        },
        "deepseek": {
            "conversation_url": task.get("conversation_url", ""),
            "send_preamble": "auto",
            "response_timeout_sec": 1200,
            "login_wait_sec": 900,
            "verification_wait_sec": 900,
        },
        "network": {
            "rate_limit_backoff_sec": [60, 180, 600, 1800],
            "max_rate_limit_retries": 6,
            "max_total_cooldown_sec": 7200,
        },
        "fields": [field["key"] for field in task["fields"]],
        "match_field": task["match_field"],
        "preamble": task["preamble"],
        "batch_header": BATCH_HEADER,
        "retry_note": RETRY_NOTE,
    }


def _kill_profile_browsers() -> None:
    try:
        import psutil
    except ImportError:
        return
    target = str(PROFILE_DIR).lower()
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name not in ("chrome.exe", "msedge.exe"):
                continue
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            if target in cmdline and "crashpad" not in cmdline:
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def _ensure_session_config() -> Path:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    dummy = SESSION_DIR / "dummy.csv"
    if not dummy.exists():
        dummy.write_text("name\n示例\n", encoding="utf-8")
    config = {
        "input": {"file": str(dummy), "sheet": 0, "column": "name", "has_header": True},
        "output": {
            "dir": str(SESSION_DIR / "output"),
            "json_name": "result.json",
            "csv_name": "result.csv",
        },
        "browser": {
            "channel": "chrome",
            "profile_dir": str(PROFILE_DIR),
            "no_proxy": True,
            "close_stale": True,
        },
        "batch": {
            "size": 10,
            "rotate_every_batches": 0,
            "max_attempts": 1,
            "split_on_parse_failure": False,
            "cooldown_sec": [0, 0],
        },
        "deepseek": {
            "conversation_url": "",
            "send_preamble": "never",
            "response_timeout_sec": 900,
            "login_wait_sec": 900,
            "verification_wait_sec": 900,
        },
        "network": {
            "rate_limit_backoff_sec": [30],
            "max_rate_limit_retries": 1,
            "max_total_cooldown_sec": 120,
        },
        "fields": ["name"],
        "match_field": "name",
        "preamble": "登录状态导入验证，不会发送任何业务消息。",
        "batch_header": BATCH_HEADER,
        "retry_note": RETRY_NOTE,
    }
    path = SESSION_DIR / "config.yaml"
    _write_yaml(path, config)
    return path


@app.before_request
def _check_token():
    if request.path.startswith("/api/") and request.method in ("POST", "DELETE", "PUT"):
        if request.headers.get("X-App-Token") != APP_TOKEN:
            return _api_error("本地访问令牌无效", 403)


@app.errorhandler(RequestEntityTooLarge)
def _too_large(_exc):
    return _api_error("文件太大（上限 100MB）", 413)


@app.errorhandler(Exception)
def _unhandled(exc):
    if isinstance(exc, HTTPException):
        return exc
    if request.path.startswith("/api/"):
        app.logger.exception("API error")
        return _api_error(f"服务器错误：{exc}", 500)
    raise exc


@app.get("/")
def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__APP_TOKEN__", APP_TOKEN)
    response = Response(html, mimetype="text/html")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/upload")
def upload():
    file = request.files.get("file")
    if file is None or not file.filename:
        return _api_error("没有收到文件")
    original = Path(file.filename).name
    suffix = Path(original).suffix.lower()
    if suffix not in (".xlsx", ".xlsm", ".csv", ".txt"):
        return _api_error("只支持 .xlsx / .xlsm / .csv / .txt")
    uid = secrets.token_hex(8)
    folder = UPLOADS / uid
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"input{suffix}"
    file.save(path)
    _write_json(folder / "meta.json", {"id": uid, "original": original, "path": str(path)})
    try:
        preview = _preview_payload(path, 0, True)
    except Exception as exc:  # noqa: BLE001
        return _api_error(f"读取文件失败：{exc}")
    return jsonify({"upload_id": uid, "original": original, **preview})


@app.post("/api/upload/<upload_id>/preview")
def upload_preview(upload_id: str):
    if not UPLOAD_RE.fullmatch(upload_id):
        return _api_error("上传编号无效", 404)
    meta = _read_json(UPLOADS / upload_id / "meta.json")
    if not meta:
        return _api_error("上传记录不存在，请重新上传", 404)
    data = request.get_json(silent=True) or {}
    try:
        preview = _preview_payload(
            Path(meta["path"]),
            data.get("sheet", 0),
            bool(data.get("has_header", True)),
        )
    except Exception as exc:  # noqa: BLE001
        return _api_error(f"读取预览失败：{exc}")
    return jsonify(preview)


@app.post("/api/preamble")
def preamble_preview():
    data = request.get_json(silent=True) or {}
    fields = data.get("fields") or []
    if not isinstance(fields, list):
        fields = []
    text = build_preamble(
        str(data.get("domain", "journal")),
        fields,
        str(data.get("match_field", "")),
        str(data.get("notes", "")),
        str(data.get("custom_prompt", "")),
    )
    return jsonify({"preamble": text})


@app.post("/api/tasks")
def create_task():
    data = request.get_json(silent=True) or {}
    upload_id = str(data.get("upload_id", ""))
    if not UPLOAD_RE.fullmatch(upload_id):
        return _api_error("上传编号无效，请重新上传文件")
    meta = _read_json(UPLOADS / upload_id / "meta.json")
    if not meta:
        return _api_error("上传记录不存在，请重新上传文件", 404)

    raw_fields = data.get("fields") or []
    fields = []
    seen = set()
    for item in raw_fields:
        key = str(item.get("key", "")).strip()
        desc = str(item.get("desc", "")).strip()
        if not FIELD_RE.fullmatch(key):
            return _api_error(f"字段名「{key}」不合法：请用英文、数字、下划线，且不以数字开头")
        if key in seen:
            return _api_error(f"字段名重复：{key}")
        seen.add(key)
        fields.append({"key": key, "desc": desc[:600]})
    if not fields:
        return _api_error("至少配置一个字段")

    match_field = str(data.get("match_field") or fields[0]["key"])
    if match_field not in seen:
        return _api_error("对齐字段必须是已配置的字段之一")

    domain = str(data.get("domain") or "journal")
    if domain not in ("journal", "scholar", "institution", "custom"):
        domain = "journal"
    notes = str(data.get("notes") or "").strip()[:1000]
    custom_prompt = str(data.get("custom_prompt") or "").strip()[:4000]
    preamble = str(data.get("preamble") or "").strip()
    if not preamble:
        if domain == "custom" and not custom_prompt:
            return _api_error("自由领域需要填写任务提示词")
        preamble = build_preamble(domain, fields, match_field, notes, custom_prompt)

    try:
        batch_size = int(data.get("batch_size", 50))
        rotate = int(data.get("rotate_every_batches", 8))
        cooldown = data.get("cooldown_sec") or [8, 25]
        cooldown_min, cooldown_max = int(cooldown[0]), int(cooldown[1])
    except (TypeError, ValueError, IndexError):
        return _api_error("批次参数格式不正确")
    if not 1 <= batch_size <= 500:
        return _api_error("每批条数需要在 1~500 之间")
    if not 0 <= rotate <= 100:
        return _api_error("轮换批次需要在 0~100 之间")
    if cooldown_min < 0 or cooldown_max < cooldown_min:
        return _api_error("等待秒数不合法")
    cooldown_max = min(cooldown_max, 3600)

    task_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    folder = _task_dir(task_id)
    input_dir = folder / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    source = Path(meta["path"])
    target = input_dir / f"input{source.suffix.lower()}"
    shutil.copy2(source, target)

    name = str(data.get("name") or Path(meta["original"]).stem).strip()[:80] or "未命名任务"
    task = {
        "id": task_id,
        "name": name,
        "created_at": _now(),
        "input_file": str(target),
        "input_name": meta["original"],
        "sheet": data.get("sheet", 0),
        "has_header": bool(data.get("has_header", True)),
        "column": data.get("column", 1),
        "fields": fields,
        "match_field": match_field,
        "batch_size": batch_size,
        "rotate_every_batches": rotate,
        "cooldown_sec": [cooldown_min, cooldown_max],
        "conversation_url": str(data.get("conversation_url", "")).strip(),
        "domain": domain,
        "notes": notes,
        "custom_prompt": custom_prompt,
        "preamble": preamble,
        "total": 0,
        "duplicates": 0,
    }
    if not task["preamble"]:
        shutil.rmtree(folder, ignore_errors=True)
        return _api_error("任务说明不能为空")

    _write_json(folder / "task.json", task)
    _write_yaml(_config_path(task_id), _build_config(task))
    try:
        cfg = load_config(_config_path(task_id))
        items, input_meta = load_items(cfg)
    except (ConfigError, InputError) as exc:
        shutil.rmtree(folder, ignore_errors=True)
        return _api_error(str(exc))
    task["total"] = len(items)
    task["duplicates"] = input_meta["duplicates"]
    _write_json(folder / "task.json", task)
    shutil.rmtree(UPLOADS / upload_id, ignore_errors=True)
    return jsonify(_public_task(task, full=True))


@app.get("/api/tasks")
def list_tasks():
    tasks = []
    if TASKS.exists():
        for folder in TASKS.iterdir():
            if not folder.is_dir() or folder.name.startswith("_"):
                continue
            task = _read_json(folder / "task.json")
            if isinstance(task, dict):
                tasks.append(_public_task(task))
    tasks.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return jsonify(tasks)


@app.get("/api/tasks/<task_id>")
def get_task(task_id: str):
    task = _load_task(task_id)
    if not task:
        return _api_error("任务不存在", 404)
    data = _public_task(task, full=True)
    data["state"] = _read_json(_task_dir(task_id) / "output" / "state.json", {})
    data["log"] = _tail_log(task_id)
    return jsonify(data)


@app.post("/api/tasks/<task_id>/start")
def start_task(task_id: str):
    task = _load_task(task_id)
    if not task:
        return _api_error("任务不存在", 404)
    proc = JOB.get("proc")
    if proc is not None and proc.poll() is None:
        other = JOB.get("task_id")
        if other == task_id:
            return _api_error("这个任务已经在运行", 409)
        return _api_error("另一个任务正在运行，请先停止它", 409)

    old_log = JOB.get("log_file")
    if old_log:
        try:
            old_log.close()
        except OSError:
            pass

    try:
        cfg = yaml.safe_load(_config_path(task_id).read_text(encoding="utf-8")) or {}
        cfg["accounts"] = _load_accounts()
        cfg["_runtime_path"] = str(_task_dir(task_id) / "runtime.json")
        _write_yaml(_config_path(task_id), cfg)
        _write_json(
            _task_dir(task_id) / "runtime.json",
            {
                "size": int(task.get("batch_size", cfg.get("batch", {}).get("size", 50))),
                "rotate_every_batches": int(
                    task.get("rotate_every_batches", cfg.get("batch", {}).get("rotate_every_batches", 8))
                ),
                "cooldown_sec": task.get("cooldown_sec", cfg.get("batch", {}).get("cooldown_sec", [8, 25])),
            },
        )
    except Exception:  # noqa: BLE001
        app.logger.exception("refresh task accounts failed")

    data = request.get_json(silent=True) or {}
    limit = data.get("limit")
    output = _task_dir(task_id) / "output"
    output.mkdir(parents=True, exist_ok=True)
    log_file = open(output / "stdout.log", "a", encoding="utf-8")
    command = [sys.executable, "-m", "src.main", "--config", str(_config_path(task_id)), "run"]
    if limit:
        try:
            command += ["--limit", str(max(1, int(limit)))]
        except (TypeError, ValueError):
            pass
    proc = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    JOB.update({"task_id": task_id, "proc": proc, "stopped": False, "log_file": log_file})
    return jsonify({"ok": True, "job": "running", "limit": limit or None})


@app.post("/api/tasks/<task_id>/settings")
def update_task_settings(task_id: str):
    task = _load_task(task_id)
    if not task:
        return _api_error("任务不存在", 404)
    data = request.get_json(silent=True) or {}
    try:
        batch_size = int(data.get("batch_size", task.get("batch_size", 50)))
    except (TypeError, ValueError):
        return _api_error("每批条数必须是整数")
    if not 1 <= batch_size <= 500:
        return _api_error("每批条数需要在 1~500 之间")
    try:
        rotate = int(data.get("rotate_every_batches", task.get("rotate_every_batches", 8)))
    except (TypeError, ValueError):
        return _api_error("会话轮换批次必须是整数")
    if not 0 <= rotate <= 100:
        return _api_error("会话轮换批次需要在 0~100 之间")
    current = task.get("cooldown_sec") or [8, 25]
    try:
        cooldown_min = float(data.get("cooldown_min", current[0]))
        cooldown_max = float(data.get("cooldown_max", current[1]))
    except (TypeError, ValueError):
        return _api_error("批间隔必须是数字")
    if not 0 <= cooldown_min <= cooldown_max <= 3600:
        return _api_error("批间隔需要满足 0 <= 最小值 <= 最大值 <= 3600")
    task.update(
        {
            "batch_size": batch_size,
            "rotate_every_batches": rotate,
            "cooldown_sec": [cooldown_min, cooldown_max],
        }
    )
    _write_json(_task_dir(task_id) / "task.json", task)
    config_path = _config_path(task_id)
    if config_path.exists():
        try:
            cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            cfg.setdefault("batch", {})
            cfg["batch"]["size"] = batch_size
            cfg["batch"]["rotate_every_batches"] = rotate
            cfg["batch"]["cooldown_sec"] = [cooldown_min, cooldown_max]
            cfg["_runtime_path"] = str(_task_dir(task_id) / "runtime.json")
            _write_yaml(config_path, cfg)
        except Exception:  # noqa: BLE001
            app.logger.exception("update task config failed")
    _write_json(
        _task_dir(task_id) / "runtime.json",
        {"size": batch_size, "rotate_every_batches": rotate, "cooldown_sec": [cooldown_min, cooldown_max]},
    )
    proc = JOB.get("proc")
    running = bool(proc is not None and proc.poll() is None and JOB.get("task_id") == task_id)
    return jsonify({"ok": True, "running": running, "task": task})


@app.post("/api/tasks/<task_id>/stop")
def stop_task(task_id: str):
    if JOB.get("task_id") != task_id or JOB.get("proc") is None:
        return _api_error("这个任务没有在运行", 409)
    JOB["stopped"] = True
    proc = JOB["proc"]
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
    _kill_profile_browsers()
    time.sleep(1)
    return jsonify({"ok": True, "job": _job_state(task_id)})


@app.get("/api/tasks/<task_id>/status")
def task_status(task_id: str):
    task = _load_task(task_id)
    if not task:
        return _api_error("任务不存在", 404)
    output = _task_dir(task_id) / "output"
    public = _public_task(task, full=True)
    return jsonify(
        {
            "task": public,
            "counts": public["counts"],
            "job": public["job"],
            "state": _read_json(output / "state.json", {}),
            "log": _tail_log(task_id),
            "has_json": (output / "result.json").exists(),
            "has_csv": (output / "result.csv").exists(),
        }
    )


@app.get("/api/tasks/<task_id>/preview")
def preview_results(task_id: str):
    task = _load_task(task_id)
    if not task:
        return _api_error("任务不存在", 404)
    try:
        limit = min(max(int(request.args.get("limit", 20)), 1), 100)
    except (TypeError, ValueError):
        limit = 20
    records_path = _task_dir(task_id) / "output" / "records.jsonl"
    last: dict[str, dict] = {}
    if records_path.exists():
        try:
            with open(records_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("id"):
                        last[entry["id"]] = entry
        except OSError:
            pass
    records = []
    for item_id, entry in last.items():
        if entry.get("status") == "ok" and isinstance(entry.get("record"), dict):
            records.append(entry)
    records.sort(key=lambda entry: int(re.sub(r"\D", "", entry["id"]) or 0))
    return jsonify(
        {
            "fields": task.get("fields", []),
            "total": len(records),
            "records": [entry["record"] for entry in records[:limit]],
        }
    )


@app.post("/api/tasks/<task_id>/export")
def export_task(task_id: str):
    task = _load_task(task_id)
    if not task:
        return _api_error("任务不存在", 404)
    proc = JOB.get("proc")
    if proc is not None and proc.poll() is None:
        return _api_error("任务运行中，先停止再导出", 409)
    result = subprocess.run(
        [sys.executable, "-m", "src.main", "--config", str(_config_path(task_id)), "export"],
        cwd=str(ROOT),
        capture_output=True,
        timeout=120,
    )
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    return jsonify({"ok": result.returncode == 0, "output": output[-4000:]})


@app.get("/api/tasks/<task_id>/download/<kind>")
def download_task(task_id: str, kind: str):
    task = _load_task(task_id)
    if not task:
        return _api_error("任务不存在", 404)
    if kind == "json":
        path = _task_dir(task_id) / "output" / "result.json"
        suffix = "json"
    elif kind == "csv":
        path = _task_dir(task_id) / "output" / "result.csv"
        suffix = "csv"
    else:
        return _api_error("下载类型无效", 404)
    if not path.exists():
        return _api_error("结果文件还没生成，请先运行或点“刷新结果”", 404)
    safe_name = re.sub(r'[\\/:*?"<>|]+', "_", task.get("name") or "result").strip() or "result"
    return send_file(
        path,
        as_attachment=True,
        download_name=f"{safe_name}.{suffix}",
    )


def _success_evidence() -> tuple[bool, str]:
    last = _read_json(SESSION_DIR / "last_import.json", {}) or {}
    if last.get("ok"):
        return True, last.get("time", "")
    paths = [ROOT / "output" / "records.jsonl"]
    if TASKS.exists():
        for folder in TASKS.iterdir():
            if folder.is_dir() and not folder.name.startswith("_"):
                paths.append(folder / "output" / "records.jsonl")
    for path in paths:
        try:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if '"status": "ok"' in text:
                when = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                return True, when
        except OSError:
            continue
    return False, ""


@app.get("/api/accounts")
def list_accounts():
    accounts = _load_accounts()
    return jsonify(
        {
            "accounts": _accounts_payload(accounts),
            "settings": {
                key: value for key, value in accounts.items() if key != "profiles"
            },
        }
    )


def _account_import_config(profile_dir: Path, slug: str) -> Path:
    folder = TASKS / "_accounts" / slug
    folder.mkdir(parents=True, exist_ok=True)
    dummy = folder / "dummy.csv"
    if not dummy.exists():
        dummy.write_text("name\n示例\n", encoding="utf-8")
    config = {
        "input": {"file": str(dummy), "sheet": 0, "column": "name", "has_header": True},
        "output": {
            "dir": str(folder / "output"),
            "json_name": "result.json",
            "csv_name": "result.csv",
        },
        "browser": {
            "channel": "chrome",
            "profile_dir": str(profile_dir),
            "no_proxy": True,
            "close_stale": True,
        },
        "batch": {
            "size": 10,
            "rotate_every_batches": 0,
            "max_attempts": 1,
            "split_on_parse_failure": False,
            "cooldown_sec": [0, 0],
        },
        "deepseek": {
            "conversation_url": "",
            "send_preamble": "never",
            "response_timeout_sec": 900,
            "login_wait_sec": 900,
            "verification_wait_sec": 900,
        },
        "network": {
            "rate_limit_backoff_sec": [30],
            "max_rate_limit_retries": 1,
            "max_total_cooldown_sec": 120,
        },
        "fields": ["name"],
        "match_field": "name",
        "preamble": "账号登录状态导入验证，不会发送业务消息。",
        "batch_header": BATCH_HEADER,
        "retry_note": RETRY_NOTE,
    }
    path = folder / "config.yaml"
    _write_yaml(path, config)
    return path


@app.post("/api/accounts")
def add_account():
    proc = JOB.get("proc")
    if proc is not None and proc.poll() is None:
        return _api_error("任务运行中，先停止任务再添加账号", 409)
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()[:40]
    if not name:
        return _api_error("请填写账号名称")
    accounts = _load_accounts()
    if any(item["name"] == name for item in accounts.get("profiles") or []):
        return _api_error("账号名称已存在")
    slug = re.sub(r"[^A-Za-z0-9_-]+", "", name) or f"acct{int(time.time())}"
    profile = ROOT / f"browser-profile-{slug}"
    base = profile
    index = 2
    while profile.exists() and not (profile / "Default").exists():
        profile = Path(f"{base}-{index}")
        index += 1
    config_path = _account_import_config(profile, slug)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "src.main", "--config", str(config_path), "import-session"],
            cwd=str(ROOT),
            capture_output=True,
            timeout=150,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "output": "导入超时：请确认剪贴板里已有登录信息。"})
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")[-8000:]
    ok = result.returncode == 0
    if ok:
        _save_account_record(name, profile)
        _save_account_status(
            [
                {
                    "name": name,
                    "profile_dir": str(profile),
                    "ok": True,
                    "url": "https://chat.deepseek.com/",
                    "error": "",
                    "checked_at": _now(),
                }
            ]
        )
    return jsonify(
        {
            "ok": ok,
            "output": output,
            "accounts": _accounts_payload(),
        }
    )


@app.post("/api/accounts/check")
def check_accounts():
    proc = JOB.get("proc")
    if proc is not None and proc.poll() is None:
        return _api_error("任务运行中，先停止任务再检查账号", 409)
    accounts = _load_accounts()
    results = []
    for index, item in enumerate(accounts.get("profiles") or [], 1):
        results.append(
            _run_account_check(item["name"], Path(item["profile_dir"]), _account_slug(item["name"], index))
        )
    _save_account_status(results, replace=True)
    return jsonify({"accounts": _accounts_payload(accounts), "checked_at": _now()})


@app.post("/api/accounts/update")
def update_account_token():
    proc = JOB.get("proc")
    if proc is not None and proc.poll() is None:
        return _api_error("任务运行中，先停止任务再更新 token", 409)
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    if not name:
        return _api_error("请指定要更新的账号")
    accounts = _load_accounts()
    profiles = accounts.get("profiles") or []
    target = next((item for item in profiles if item["name"] == name), None)
    if target is None:
        return _api_error("账号不存在", 404)
    profile = Path(target["profile_dir"])
    slug = _account_slug(name, profiles.index(target) + 1)
    config_path = _account_import_config(profile, slug)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "src.main", "--config", str(config_path), "import-session"],
            cwd=str(ROOT),
            capture_output=True,
            timeout=150,
        )
    except subprocess.TimeoutExpired:
        return jsonify(
            {
                "ok": False,
                "output": "导入超时：请确认剪贴板里已有该账号新的登录信息。",
                "accounts": _accounts_payload(accounts),
            }
        )
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")[-8000:]
    ok = result.returncode == 0
    if ok:
        check = _run_account_check(name, profile, slug)
        _save_account_status([check])
        ok = bool(check["ok"])
        if not ok:
            output += "\n[提示] token 已写入，但检查后仍处于未登录状态，请确认 token 完整、有效。"
    return jsonify(
        {
            "ok": ok,
            "output": output,
            "accounts": _accounts_payload(),
        }
    )


@app.post("/api/accounts/login")
def account_login():
    proc = JOB.get("proc")
    if proc is not None and proc.poll() is None:
        return _api_error("任务运行中，先停止任务再登录账号", 409)
    current = LOGIN_JOB.get("proc")
    if current is not None and current.poll() is None:
        return _api_error("已有登录窗口在等待完成，请先完成或关闭它", 409)
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    if not name:
        return _api_error("请指定要登录的账号")
    accounts = _load_accounts()
    profiles = accounts.get("profiles") or []
    target = next((item for item in profiles if item["name"] == name), None)
    if target is None:
        return _api_error("账号不存在", 404)
    profile = Path(target["profile_dir"])
    slug = _account_slug(name, profiles.index(target) + 1)
    config_path = _account_login_config(profile, slug)
    login_proc = subprocess.Popen(
        [sys.executable, "-m", "src.main", "--config", str(config_path), "login-watch"],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    LOGIN_JOB["name"] = name
    LOGIN_JOB["proc"] = login_proc
    try:
        raw, _ = login_proc.communicate(timeout=700)
    except subprocess.TimeoutExpired:
        login_proc.kill()
        raw, _ = login_proc.communicate()
    finally:
        LOGIN_JOB["proc"] = None
        LOGIN_JOB["name"] = ""
    output = raw.decode("utf-8", errors="replace")[-8000:]
    ok = False
    for line in output.splitlines():
        if line.startswith("__LOGIN_RESULT__"):
            try:
                ok = bool(json.loads(line[len("__LOGIN_RESULT__") :]).get("ok"))
            except Exception:  # noqa: BLE001
                ok = False
    if ok:
        check = _run_account_check(name, profile, slug)
        _save_account_status([check])
        ok = bool(check["ok"])
        if not ok:
            output += "\n[提示] 检测到登录，但复查未通过，请确认窗口里已进入聊天页。"
    return jsonify({"ok": ok, "output": output, "accounts": _accounts_payload()})


@app.get("/api/session/status")
def session_status():
    ok, when = _success_evidence()
    return jsonify(
        {
            "ok": ok,
            "time": when,
            "running_job": bool(JOB.get("proc") and JOB["proc"].poll() is None),
        }
    )


@app.post("/api/session/import")
def import_session():
    proc = JOB.get("proc")
    if proc is not None and proc.poll() is None:
        return _api_error("任务运行中，先停止任务再导入登录状态", 409)
    config_path = _ensure_session_config()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "src.main", "--config", str(config_path), "import-session"],
            cwd=str(ROOT),
            capture_output=True,
            timeout=150,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "output": "导入超时：请确认剪贴板里已有登录信息，然后重试。"})
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")[-8000:]
    ok = result.returncode == 0
    _write_json(SESSION_DIR / "last_import.json", {"ok": ok, "time": _now(), "output": output})
    screenshot = SESSION_DIR / "output" / "import_session.png"
    return jsonify(
        {
            "ok": ok,
            "output": output,
            "screenshot": "/api/session/screenshot" if screenshot.exists() else "",
        }
    )


@app.get("/api/session/screenshot")
def session_screenshot():
    path = SESSION_DIR / "output" / "import_session.png"
    if not path.exists():
        return _api_error("没有截图", 404)
    return send_file(path, mimetype="image/png")


def _find_port(start: int) -> int:
    for port in range(start, start + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="DeepSeek 批量信息采集控制台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    TASKS.mkdir(parents=True, exist_ok=True)
    port = _find_port(args.port)
    url = f"http://127.0.0.1:{port}"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"\n控制台已启动：{url}\n按 Ctrl+C 可以关闭控制台。\n")
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
