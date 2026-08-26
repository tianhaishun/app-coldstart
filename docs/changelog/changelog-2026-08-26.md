# 变更日志 — 2026-08-26（UI 全面重设计：Linear 化）

## 背景与流程

用户对旧版（OC-2 暖橙 + 全页等宽 + 满屏边框）不满意，要求整体重设计「Linear 质感」。
依用户要求采用**隔离演示制**：`ui-redesign` 分支 + git worktree 独立目录 + 8767 端口并跑，
每阶段三联对比（现状/新版/Linear 参考）验收，零污染主工作区。
用户验收通过后回合并入 ui 分支。

## 已交付（Demo-1 + Demo-2）

### P0 换肤
- 新色源管线：`static/themes/linear.json`（Linear 暗色值）→ `_bake_linear.py` → `linear.css`
  （替代 OC-2：`oc-2.json/css/_bake_oc2.py` 已删除）
- 兼容别名层同名重映射（`--primary`→紫 #5e6ad2、`--mint`→哑绿 #4cb782、`--outline-var`→发丝线），
  页面 JS/既有 CSS 零感知换肤
- **Inter Variable woff2 自托管**（static/fonts/，48KB，OFL 许可，零网络依赖）
- 仅暗色：#themeLightBtn 保留但隐藏；applyUiPrefs 强制 dark（前 light 偏好静默回落）

### P1 排版
- 6 级字阶 tokens（--text-display/value/title/body/label/data），计时器 64px tabular-nums
- UI 字体一律 Inter；**data-font 只作用于 .font-mono/.mono 数据层**（修复旧版「整页等宽」）
- 去边框化：卡片无边框无投影，改表面亮度阶梯；按钮三级制（紫填充/亮面/幽灵）
- 输入框/焦点环统一 4px 圆角 + 紫晕

### P2 纪律
- 状态徽章→无底色小圆点 + 中性文字（绿点收敛）
- 选中态语义归位：tertiary 从「成功绿」改映射到强调紫（选中≠成功）
- 全交互元素 150ms 过渡 + 按压 scale(0.98) + :focus-visible 紫环

### P3 收敛
- 顶栏只留【设备/项目/⚙/状态】；主题/等宽/字号/直播收进设置弹层（**id 零变更**）
- 统计区重画：去「—s」坏占位、空态弱色引导文案（等待首次/二次冷启动样本）
- 历史区空态留白重画

## 过程中抓到并修复的 bug

- 弹层 ID 选择器 `display:flex` 压过 `.hidden` 的 `display:none` → 设置面板默认常开
  （实拍截图发现）；修正为显式状态机 `#settingsPanel:not(.hidden) { display: flex; }`

## 合并方式

- ui 分支先提交未落地的「方案C 审核修复」（9efac1e），再 ff 并入 ui-redesign（a478d70）
- index.html 自动合并无冲突：审核 JS 修复（apiFetch/CSV 转义/Delete 确认/忙碌徽标/
  scrcpy 异常保护）与 Linear CSS 并存，diff 校验零 id 删除，node --check 通过

## 验证记录

- pytest 90 passed（58 存量 + 32 审核新增）
- node --test 7 passed；index.html 内嵌 JS node --check 通过
- 8767 分支站实测：主题/字体/弹层/统计空态全链路人工+截图核对
- AGENTS.md §3.3.1 已同步为新规范（Linear 令牌/Inter/字阶 6 级/仅暗色/烘焙管线）

## 遗留说明

- 亮色模式未实现（Linear 以暗色为主）；如需后续补 Linear light 值即可扩展 linear.json
- 8766 后端常驻（合并后已重启）
