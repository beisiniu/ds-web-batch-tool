from __future__ import annotations

import csv
import hashlib
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook


class InputError(RuntimeError):
    pass


@dataclass
class Item:
    id: str
    value: str
    row: int


def _clean(value) -> str:
    if value is None:
        return ""
    text = str(value)
    text = text.replace("\u200b", "").replace("\ufeff", "").replace("\u3000", " ")
    return text.strip()


def _resolve_column(header: list[str], column, has_header: bool) -> int:
    if isinstance(column, int):
        idx = column - 1
    elif isinstance(column, str) and column.strip().isdigit():
        idx = int(column.strip()) - 1
    elif has_header:
        target = _clean(column)
        idx = -1
        for i, name in enumerate(header):
            if _clean(name) == target:
                idx = i
                break
        if idx < 0:
            raise InputError(
                f"表头里找不到列「{column}」。现有表头：{header}"
            )
    else:
        raise InputError("未启用表头时，column 请写列序号（如 1）")
    if idx < 0:
        raise InputError(f"列序号不合法：{column}")
    return idx


def _read_xlsx(path: Path, sheet, column, has_header: bool):
    with open(path, "rb") as fh:
        wb = load_workbook(fh, read_only=True, data_only=True)
        try:
            if isinstance(sheet, str) and not sheet.isdigit():
                if sheet not in wb.sheetnames:
                    raise InputError(f"工作表「{sheet}」不存在。现有：{wb.sheetnames}")
                ws = wb[sheet]
            else:
                idx = int(sheet)
                if idx >= len(wb.worksheets):
                    raise InputError(f"工作表序号 {idx} 超出范围，共 {len(wb.worksheets)} 个")
                ws = wb.worksheets[idx]

            rows = ws.iter_rows(values_only=True)
            first = next(rows, None)
            if first is None:
                return [], []
            first = list(first)
            idx = _resolve_column([_clean(x) for x in first], column, has_header)
            data_rows = rows if has_header else iter([tuple(first), *rows])
            start_row = 2 if has_header else 1
            values = []
            for offset, row in enumerate(data_rows):
                cell = row[idx] if idx < len(row) else None
                text = _clean(cell)
                if text:
                    values.append((start_row + offset, text))
            return values, [_clean(x) for x in first]
        finally:
            wb.close()


def _detect_encoding(path: Path) -> str:
    raw = path.read_bytes()[:200000]
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "utf-8"


def _read_csv(path: Path, column, has_header: bool):
    enc = _detect_encoding(path)
    with open(path, "r", encoding=enc, newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return [], []
    header = [_clean(x) for x in rows[0]]
    idx = _resolve_column(header, column, has_header)
    data_rows = rows[1:] if has_header else rows
    start_row = 2 if has_header else 1
    values = []
    for offset, row in enumerate(data_rows):
        cell = row[idx] if idx < len(row) else ""
        text = _clean(cell)
        if text:
            values.append((start_row + offset, text))
    return values, header


def _looks_like_xlsx(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"PK\x03\x04"
    except OSError:
        return False


def load_items(cfg: dict) -> tuple[list[Item], dict]:
    path = Path(cfg["input"]["file"])
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm") or (suffix in (".csv", ".txt") and _looks_like_xlsx(path)):
        values, header = _read_xlsx(
            path, cfg["input"].get("sheet", 0), cfg["input"]["column"], bool(cfg["input"]["has_header"])
        )
    elif suffix in (".csv", ".txt"):
        values, header = _read_csv(path, cfg["input"]["column"], bool(cfg["input"]["has_header"]))
    else:
        raise InputError(f"不支持的文件类型：{suffix}（请用 xlsx / csv / txt）")

    if not values:
        raise InputError("表格里没有读到有效名称，请检查列名和 has_header 设置")

    items = [Item(id=f"r{row}", value=value, row=row) for row, value in values]
    normalized = [unicodedata.normalize("NFKC", i.value) for i in items]
    fingerprint = hashlib.sha1("\n".join(normalized).encode("utf-8")).hexdigest()

    duplicates = len(items) - len(set(normalized))
    meta = {
        "path": str(path),
        "header": header,
        "total": len(items),
        "duplicates": duplicates,
        "fingerprint": fingerprint,
    }
    return items, meta
