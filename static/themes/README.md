# OpenCode OC-2 主题（本项目唯一色源）

- **主题**：OC-2（`id: oc-2`）
- **锁定上游**：`anomalyco/opencode` commit **`ec3ae17e`**
- **源文件**：`packages/ui/src/theme/themes/oc-2.json` → 本目录 `oc-2.json`
- **产物**：`oc-2.css`（由 JSON 的 `palette` + `overrides` 展开；含本项目兼容别名）

## 规则

1. **禁止**在 `index.html` / 组件样式里手填业务色 hex。
2. 改色：更新上游 JSON（换 commit 后覆盖 `oc-2.json`）→ 重新生成 `oc-2.css`。
3. 本阶段不移植 OpenCode 全量 OKLCH `resolve.ts`；只用 JSON 作者值。

## 重新生成 CSS

```bash
python static/themes/_bake_oc2.py
```

不要手改 `oc-2.css` 里的 hex。
