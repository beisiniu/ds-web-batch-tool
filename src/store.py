from __future__ import annotations

import csv
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from .parse import normalize_name


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Store:
    def __init__(self, outdir: Path, cfg: dict):
        self.dir = Path(outdir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cfg = cfg
        self.records_path = self.dir / "records.jsonl"
        self.state_path = self.dir / "state.json"
        self.raw_dir = self.dir / "raw"
        self.raw_dir.mkdir(exist_ok=True)
        self._last: dict[str, dict] = self.last_entries()

    def last_entries(self) -> dict[str, dict]:
        result: dict[str, dict] = {}
        if not self.records_path.exists():
            return result
        with open(self.records_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("id"):
                    result[entry["id"]] = entry
        return result

    def is_done(self, item_id: str) -> bool:
        entry = self._last.get(item_id)
        return bool(entry and entry.get("status") in ("ok", "failed"))

    def append(self, entries: list[dict]) -> None:
        if not entries:
            return
        with open(self.records_path, "a", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        for entry in entries:
            if entry.get("id"):
                self._last[entry["id"]] = entry

    def save_raw(self, label: str, attempt: int, reply) -> str | None:
        safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", label)
        path = self.raw_dir / f"{safe}_a{attempt}.txt"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("=== DOM CODE ===\n")
                f.write(reply.dom_code or "")
                f.write("\n\n=== DOM FULL ===\n")
                f.write(reply.dom_full or "")
                f.write("\n\n=== SSE ===\n")
                f.write(reply.sse or "")
                f.write("\n")
            return str(path.relative_to(self.dir))
        except OSError:
            return None

    def load_state(self) -> dict:
        if not self.state_path.exists():
            return {}
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def save_state(self, state: dict) -> None:
        state["updated_at"] = _now()
        tmp = self.state_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path)

    def counts(self, items) -> dict:
        ok = failed = pending = 0
        for item in items:
            entry = self._last.get(item.id)
            status = entry.get("status") if entry else None
            if status == "ok":
                ok += 1
            elif status == "failed":
                failed += 1
            else:
                pending += 1
        return {"ok": ok, "failed": failed, "pending": pending, "total": len(items)}

    def archive_existing(self) -> Path | None:
        if not self.records_path.exists() and not self.state_path.exists():
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.dir / "archive" / stamp
        target.mkdir(parents=True, exist_ok=True)
        for path in (self.records_path, self.state_path):
            if path.exists():
                shutil.move(str(path), str(target / path.name))
        if self.raw_dir.exists():
            shutil.move(str(self.raw_dir), str(target / "raw"))
        self.raw_dir.mkdir(exist_ok=True)
        self._last = {}
        return target

    def export(
        self,
        items,
        fields: list[str],
        json_name: str,
        csv_name: str,
        include_missing: bool = False,
    ) -> dict:
        records: list[dict] = []
        missing: list[dict] = []
        extra_keys: list[str] = []

        for item in items:
            entry = self._last.get(item.id)
            if entry and entry.get("status") == "ok" and isinstance(entry.get("record"), dict):
                record = entry["record"]
                records.append(record)
                for key in record:
                    if key not in fields and key not in extra_keys:
                        extra_keys.append(key)
            else:
                status = entry.get("status") if entry else "pending"
                missing.append(
                    {
                        "id": item.id,
                        "input_name": item.value,
                        "status": status or "pending",
                        "reason": (entry or {}).get("error", ""),
                    }
                )
                if include_missing:
                    placeholder = {field: "" for field in fields}
                    placeholder["input_name"] = item.value
                    placeholder["_status"] = status or "pending"
                    records.append(placeholder)

        json_path = self.dir / json_name
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)

        csv_path = self.dir / csv_name
        header = ["input_name"] + fields + extra_keys
        by_name = {}
        for item in items:
            entry = self._last.get(item.id)
            if entry and entry.get("status") == "ok" and isinstance(entry.get("record"), dict):
                by_name[normalize_name(item.value)] = entry["record"]
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            for item in items:
                record = by_name.get(normalize_name(item.value))
                if record is None:
                    continue
                row = [item.value]
                for key in fields + extra_keys:
                    row.append(record.get(key, ""))
                writer.writerow(row)

        missing_path = self.dir / "missing.csv"
        with open(missing_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "input_name", "status", "reason"])
            for row in missing:
                writer.writerow([row["id"], row["input_name"], row["status"], row["reason"]])

        return {
            "json": str(json_path),
            "csv": str(csv_path),
            "missing": str(missing_path),
            "ok": len(records) if not include_missing else len(records) - len(missing),
            "missing_count": len(missing),
        }
