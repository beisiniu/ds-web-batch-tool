from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml


class ConfigError(RuntimeError):
    pass


DEFAULTS: dict[str, Any] = {
    "input": {"sheet": 0, "column": 1, "has_header": True},
    "browser": {
        "no_proxy": True,
        "close_stale": True,
        "channel": "auto",
        "profile_dir": "browser-profile",
    },
    "output": {"dir": "output", "json_name": "result.json", "csv_name": "result.csv"},
    "batch": {
        "size": 50,
        "rotate_every_batches": 10,
        "max_attempts": 3,
        "split_on_parse_failure": True,
        "cooldown_sec": [8, 25],
    },
    "deepseek": {
        "conversation_url": "",
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
    "fields": [],
    "match_field": "",
    "preamble": "",
    "batch_header": "第 {batch_no} 批，共 {count} 个。请按约定输出 JSON 数组：",
    "retry_note": "上一次输出无法解析为 JSON 数组，请重新输出本批全部结果，只输出 JSON 数组。",
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path) -> dict[str, Any]:
    path = Path(path).resolve()
    if not path.exists():
        raise ConfigError(f"找不到配置文件：{path}")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    cfg = _merge(DEFAULTS, raw)
    cfg["_config_path"] = str(path)
    cfg["_root"] = str(path.parent)
    _validate(cfg)
    return cfg


def _validate(cfg: dict) -> None:
    inp = cfg["input"]
    if not inp.get("file"):
        raise ConfigError("config.yaml 里没有填写 input.file")

    input_path = Path(inp["file"])
    if not input_path.is_absolute():
        input_path = Path(cfg["_root"]) / input_path
    if not input_path.exists():
        raise ConfigError(f"输入文件不存在：{input_path}")
    inp["file"] = str(input_path)

    size = int(cfg["batch"]["size"])
    if size < 1 or size > 500:
        raise ConfigError("batch.size 需要在 1~500 之间")
    cfg["batch"]["size"] = size

    if cfg["deepseek"]["send_preamble"] not in ("auto", "always", "never"):
        raise ConfigError("deepseek.send_preamble 只能是 auto / always / never")
    if not str(cfg.get("preamble", "")).strip():
        raise ConfigError("preamble 不能为空：需要告诉 AI 输出什么字段和格式")

    cooldown = cfg["batch"]["cooldown_sec"]
    if not isinstance(cooldown, list) or len(cooldown) != 2:
        raise ConfigError("batch.cooldown_sec 需要写成 [最小秒数, 最大秒数]")

    if not cfg.get("fields"):
        raise ConfigError("fields 不能为空：请列出 AI 应返回的字段名")
    if not cfg.get("match_field"):
        cfg["match_field"] = cfg["fields"][0]


def project_root(cfg: dict) -> Path:
    return Path(cfg["_root"])


def output_dir(cfg: dict) -> Path:
    p = Path(cfg["output"]["dir"])
    if not p.is_absolute():
        p = project_root(cfg) / p
    return p
