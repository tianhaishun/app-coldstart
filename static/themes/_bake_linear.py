# -*- coding: utf-8 -*-
"""从 linear.json 生成 linear.css（2026-08 UI 重设计，替代 oc-2 管线）。

色源仅来自 JSON palette + overrides，本脚本不含任何裸色值。
兼容别名层保持与 oc-2 时代同名（--bg/--primary/--mint/--outline-var 等），
index.html 的 JS 与既有 CSS 零感知换肤。
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    data = json.loads((HERE / "linear.json").read_text(encoding="utf-8"))
    p = data["dark"]["palette"]
    ov = data["dark"].get("overrides", {})

    lines = [
        "/* Linear theme tokens",
        " * SOURCE: static/themes/linear.json（应用侧唯一色源，2026-08 重设计）",
        " * GENERATED — DO NOT hand-edit hex here — change linear.json then regenerate:",
        " *   python static/themes/_bake_linear.py",
        " * 仅暗色（亮色切换已隐藏）；字体 Inter 自托管（OFL），见 static/fonts/。",
        " */",
        "",
        "@font-face {",
        "  font-family: 'Inter';",
        "  font-style: normal;",
        "  font-weight: 100 900;",
        "  font-display: swap;",
        "  src: url('../fonts/inter-latin-wght-normal.woff2') format('woff2-variations');",
        "}",
        "",
        'html[data-theme="linear"][data-color-scheme="dark"] {',
        "  color-scheme: dark;",
        "  /* ── 原始 palette ── */",
        f"  --l-bg: {p['bg']};",
        f"  --l-panel: {p['panel']};",
        f"  --l-panel-low: {p['panel-low']};",
        f"  --l-raised: {p['raised']};",
        f"  --l-raised-hover: {p['raised-hover']};",
        f"  --l-accent: {p['accent']};",
        f"  --l-accent-hover: {p['accent-hover']};",
        f"  --l-success: {p['success']};",
        f"  --l-warning: {p['warning']};",
        f"  --l-danger: {p['danger']};",
        f"  --l-text-strong: {p['text-strong']};",
        f"  --l-text-base: {p['text-base']};",
        f"  --l-text-weak: {p['text-weak']};",
        f"  --l-text-weaker: {p['text-weaker']};",
        f"  --l-hairline: {p['hairline']};",
        f"  --l-hairline-weak: {p['hairline-weak']};",
        f"  --l-ink-on-accent: {p['ink-on-accent']};",
        "  /* ── overrides ── */",
    ]
    for k, v in ov.items():
        lines.append(f"  --{k}: {v};")

    lines += [
        "",
        "  /* ── 兼容别名层（与 oc-2 时代同名，页面零感知换肤）── */",
        "  --bg: var(--l-bg);",
        "  --sc-lowest: var(--l-bg);",
        "  --sc-low: var(--l-panel-low);",
        "  --sc: var(--l-panel);",
        "  --sc-high: var(--l-raised);",
        "  --sc-highest: var(--l-raised-hover);",
        "  --surface-container-lowest: var(--l-bg);  /* 修复旧引用（原为拼错名）*/",
        "  --on: var(--l-text-strong);",
        "  --on-var: var(--l-text-base);",
        "  --outline-var: var(--l-hairline);",
        "  --outline-variant: var(--l-hairline);      /* 修复旧引用 */",
        "  --border-weak-base: var(--l-hairline);",
        "  --border-weaker-base: var(--l-hairline-weak);",
        "  --primary: var(--l-accent);",
        "  --primary-brand: var(--l-accent);",
        "  --primary-hover: var(--l-accent-hover);",
        "  --primary-c: color-mix(in srgb, var(--l-accent) 12%, transparent);",
        "  --primary-soft: color-mix(in srgb, var(--l-accent) 15%, transparent);",
        "  --on-primary-c: var(--l-text-strong);",
        "  --mint: var(--l-success);",
        "  --green: var(--l-success);",
        "  --amber: var(--l-warning);",
        "  --danger: var(--l-danger);",
        "  --palette-ink: var(--l-ink-on-accent);",
        "  --palette-neutral: var(--l-ink-on-accent);",
        "  --palette-primary: var(--l-accent);",
        "  --glass: var(--l-panel);",
        "",
        "  /* ── 字体（Inter 自托管 + 系统回退；mono 仅数据/日志）── */",
        '  --font-ui: \'Inter\', \'Segoe UI Variable Text\', \'Segoe UI\', \'Microsoft YaHei\', \'PingFang SC\', sans-serif;',
        '  --font-mono: \'Cascadia Mono\', \'JetBrains Mono\', Consolas, \'Courier New\', monospace;',
        "",
        "  /* ── 字阶（6 级，全页仅允许这些字号 token）── */",
        "  --text-display: 3.5rem;    /* 56px 计时器 */",
        "  --text-value: 1.25rem;     /* 20px 统计大数 */",
        "  --text-title: 0.8125rem;   /* 13px 卡片标题 */",
        "  --text-body: 0.78125rem;   /* 12.5px 正文/按钮 */",
        "  --text-label: 0.65625rem;  /* 10.5px 大写标签 */",
        "  --text-data: 0.71875rem;   /* 11.5px 数据/日志 */",
        "}",
        "",
    ]
    out = HERE / "linear.css"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
