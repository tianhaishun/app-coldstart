# -*- coding: utf-8 -*-
"""从 oc-2.json 生成 oc-2.css。色源仅来自 JSON palette + overrides。"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMMIT = "ec3ae17e"


def norm(v: str) -> str:
    # 上游 light.icon-weak-base 缺 #，补齐以便 CSS 合法
    if isinstance(v, str) and len(v) in (6, 8) and not v.startswith("#") and all(
        c in "0123456789abcdefABCDEF" for c in v
    ):
        return "#" + v
    return v


def block(scheme: str, variant: dict) -> str:
    lines = [
        f'html[data-theme="oc-2"][data-color-scheme="{scheme}"] {{',
        f"  color-scheme: {scheme};",
    ]
    for k, v in variant["palette"].items():
        lines.append(f"  --palette-{k}: {norm(v)};")
    for k, v in variant.get("overrides", {}).items():
        lines.append(f"  --{k}: {norm(v)};")
    # 兼容别名：旧 Material 式变量 → OC-2（不另造色相）
    lines += [
        "  /* compat aliases → OC-2 */",
        "  --bg: var(--surface-base);",
        "  --sc-lowest: var(--surface-base);",
        "  --sc-low: var(--surface-raised-base);",
        "  --sc: var(--surface-raised-base);",
        "  --sc-high: var(--surface-raised-base-hover);",
        "  --sc-highest: var(--border-weak-base);",
        "  --on: var(--text-strong);",
        "  --on-var: var(--text-base);",
        "  --outline-var: var(--border-weak-base);",
        "  --primary: var(--palette-interactive);",
        "  --primary-c: var(--palette-interactive);",
        "  --primary-brand: var(--palette-primary);",
        "  --on-primary-c: var(--palette-ink);",
        "  --mint: var(--palette-success);",
        "  --green: var(--icon-success-base, var(--palette-success));",
        "  --amber: var(--palette-warning);",
        "  --danger: var(--palette-error);",
        "  --primary-soft: color-mix(in srgb, var(--palette-interactive) 18%, transparent);",
        "  --glass: color-mix(in srgb, var(--surface-raised-base) 88%, transparent);",
        "  --radius: 4px;",
        "}",
    ]
    return "\n".join(lines)


def main() -> None:
    data = json.loads((HERE / "oc-2.json").read_text(encoding="utf-8"))
    css = "\n".join(
        [
            "/* OpenCode OC-2 theme tokens",
            f" * SOURCE: anomalyco/opencode@{COMMIT}",
            " * FILE: packages/ui/src/theme/themes/oc-2.json",
            " * GENERATED from palette + overrides only (no OKLCH resolve fork).",
            " * DO NOT hand-edit hex here — change oc-2.json then regenerate.",
            " */",
            "",
            block("light", data["light"]),
            "",
            block("dark", data["dark"]),
            "",
        ]
    )
    out = HERE / "oc-2.css"
    out.write_text(css, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
