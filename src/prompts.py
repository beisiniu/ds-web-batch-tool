from __future__ import annotations

import hashlib


def render(template: str, **values) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def build_batch_message(cfg: dict, items, batch_no: int, total: int, remaining: int) -> str:
    header = render(
        cfg["batch_header"],
        batch_no=batch_no,
        count=len(items),
        total=total,
        remaining=remaining,
    ).rstrip()
    lines = [f"{index}. {item.value}" for index, item in enumerate(items, 1)]
    token = hashlib.sha1("\n".join(item.value for item in items).encode("utf-8")).hexdigest()[:8]
    token_line = f"【批次校验码 {token}（仅用于断点续跑，请忽略）】"
    return f"{header}\n\n" + "\n".join(lines) + f"\n\n{token_line}"
