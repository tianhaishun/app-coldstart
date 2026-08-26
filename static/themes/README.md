# Linear 主题管线（本项目唯一色源，2026-08 UI 重设计）

- **色源**：`linear.json`（Linear 暗色设计值；仅暗色，亮色切换已隐藏）
- **生成**：`python static/themes/_bake_linear.py` → 产出 `linear.css`
- **运行**：`index.html` 以 `data-theme="linear"` + `data-color-scheme="dark"` 消费；页面只要 `var(--…)` 别名（`--bg/--primary/--mint/--outline-var` 等），换肤零感知

## 操作规则

1. **禁止**在 `index.html` / 组件样式里手填业务色 hex；只消费 `var(--…)`
2. 改色流程：改 `linear.json` → 运行 `python static/themes/_bake_linear.py` → 硬刷新验证
3. **强调色纪律**：`--primary`（紫 #5e6ad2）唯一饱和色；状态色只以 ≤12% alpha 底纹 / 圆点出现
4. **字体**：`--font-ui` = Inter Variable（自托管 `static/fonts/`，OFL）+ 系统回退；`--font-mono` 只管数据/日志；UI 控件禁止 mono
5. 字阶 6 级 token（`--text-display/value/title/body/label/data`），禁止 tokens 之外的裸 px

> 历史：OC-2 管线（oc-2.json/_bake_oc2.py）已于 2026-08 全面移除，换肤为 Linear。
