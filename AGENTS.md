# App 冷启测速 - Agent 工作规则

本文件是本项目（D:\work\tool\app-coldstart）的硬规范。
任何 agent（Claude / 其他 AI / 人类协作者）进入本项目前必须先读完。
冲突优先级：用户当次明确指令 > 本文件 > agent 私有记忆。

---

## 1. 行动准则（最高优先级）

### 1.1 任何改动前必须先列计划、经用户确认
**禁止**未讨论就动手。哪怕是"看起来很小"的修改，也要先：
1. 用 grep/read 看清楚要改的代码在整体里扮演什么角色
2. 用一段话陈述"我准备做什么、为什么、改哪些文件、可能影响什么"
3. 等用户明确同意后再动手

**禁止**"顺手改一下"。"顺手"是 bug 的温床。

### 1.2 读懂现有代码再改
任何函数、任何字段、任何换算公式，**改之前先用真实数据验证它当前是怎么工作的**。
不能凭"看起来不对"就改——可能它是对的，是改的人（包括你自己）没看懂。

### 1.3 不要新增字段、不要重命名、不要改契约，除非用户同意
- 后端 API 响应字段 = 前后端契约，改一个字段名要两边一起改、一起测
- 函数签名、文件名、目录结构同理

### 1.4 改完必须验证
任何核心逻辑（计时、坐标换算、统计算法）改动后，**必须用真实数据或 mock 跑一遍**，
对比期望值。不能口头说"应该对"，要给出数字证据。

---

## 2. 项目核心逻辑（不可随意改动）

以下逻辑是项目命脉，**改之前必须理解它的原始设计意图**：

### 2.1 计时原理（v1 逻辑，已验证 correct）
- 前端按 Space → `fetch('/api/cold_start')` → 服务端编排 force_stop + HOME + tap → 响应返回
- 响应回来后前端直接 `startTs = performance.now()`（单一时钟，不校准）
- 墙钟经过：`rawMs = performance.now() - startTs`
- **自动测速命中停表时**：记入样本 `time = max(0, rawMs - shot_ms)`，其中 `shot_ms` 为**命中那一帧**后端实测的「截图命令→拿到画面」耗时（成功画面在截图开始时已在屏上，传图不应计入冷启）。跳过弹窗帧不停表，不扣。
- **单一 performance.now() 时钟**；扣的是同一次响应里带回的时长字段，**不是**用手机/服务器墙钟去减起点
- 服务端 cold_start 仍返回 `start_wall` 字段（记录 tap 发出时刻，供诊断/将来用），但前端计时**不消费它**
- 不要引入 `Date.now() - start_wall*1000` 这类 wall 校准——v2 曾试过，会把 tap 命令执行时间误当网络延迟，导致起跳显示 0.6s（详见 §6 历史教训）

### 2.2 坐标换算
- 后端 `tap_norm(cx, cy)` → `tap_pixel(int(cx*w), int(cy*h))`，`w/h` 来自 `wm size`
- 前端用户点击画面像素 → 归一化坐标 → 后端 → 设备像素，往返一致
- 改任何一边的换算公式前，必须先用 mock 验证去程=回程

### 2.3 去极值均值统计算法
- **分组规则**：按显示序号（1-based）交替分组。#01=首次冷启动，#02=二次冷启动，#03=首次...以此类推。这个交替约定依赖用户的操作顺序：用户必须按"卸装→测首次→测二次→卸装→..."的节奏采集，否则分组错。
- **组内截尾**：n≥3 时去掉一个最大值、一个最小值，剩余取平均；n<3 时全部取平均。
- **并列处理**：所有值相同的情况下，maxIdx == minIdx（都指向第一个匹配），filter 会过滤掉同一个 idx 一次，剩 n-1 个值取均——这是已知行为，不要"修"。
- **平台调整**：iOS 首次冷启动最终均值 -1 秒（剔除 TestFlight 测试弹窗时间）；GP（安卓）不调整；二次冷启动不减。
- **保留老逻辑**：UI 同时显示"全部一锅烩去极值均值"（老逻辑），作对照参考。报告/CSV 里两种都输出。
- 每组剔除标记在历史列表里实时显示（[首]/[二] 标签 + MAX/MIN 标记）；老的一锅烩剔除标记如果与分组剔除冲突，以分组为准（实色），老的用半透明 + `*` 标注。

### 2.4 adb 调用
- 自动测速热路径截图优先 `exec-out sh -c 'screencap | gzip -1 -c'`（raw+gzip，免设备 PNG 编码；Pixel 6a 实测快于 `-p`），失败回退 `screencap -p`；落盘/模板仍可用 PNG
- 所有 adb 调用集中在 `AdbDevice` 类，便于加锁 + 复用
- Session 的 `_lock` 串行化所有设备 I/O（adb server 不擅长并发）
- **所有会触发 adb 的端点必须在持锁状态下访问设备**（审核修复）。
  统一入口是 ``with SESSION.device_op() as dev:``；等效写法是 ``with SESSION._lock:`` + ``SESSION.device.xxx()``
  （两者持同一把 RLock，功能完全等价）。不能在**不持锁**的情况下裸访问 ``SESSION.device``。
- `run(check=False)` 会吞掉非零退出码，只用于"失败可接受"场景（force_stop 杀不存在的进程）。
  关键操作（uninstall/install）必须验证输出含 Success/Failure，空输出（device offline）要抛错。

### 2.5 模板比对停表（v3 新增，自动测速核心）
- 用户点画面选定"启动元素"→ 后端以该坐标为中心截 240×120 小区域存为模板（`_cst_marker.png`）
- 运行时 `check_marker` 截当前屏 + 在模板坐标 ±20px 范围用 `cv2.matchTemplate(TM_CCOEFF_NORMED)` 搜索
- 置信度 ≥ 0.85 且满足上升沿后停表（默认连续 **1** 帧确认；截图贵，2 帧会多等一整轮）
- **上升沿**：须先见过低于阈值的帧再过阈才停，防桌面残留误停。例外：`cold_start` 在 force_stop 后 `reset_marker_watch(after_force_stop=True)` 直接种 below——刚杀进程不可能还在成功页；否则二次冷启动无 SKIP、首帧已过阈会永远卡住
- **matchTemplate 部分 3ms/次**；热路径截图优先 `screencap|gzip`（Pixel 6a ~350ms），PNG `-p` 作回退（~580ms）
- **纯色模板必须拒绝**（灰度标准差 < 15）：TM_CCOEFF_NORMED 对纯色返回 1.0 满置信度会误命中
- 模板与设备/分辨率绑定，重启后端会清掉（Session._marker_template 是内存变量）
- **不用 OCR 全图文字匹配做停表**（实测 RapidOCR 全图推理 1373ms/次，精度 ±1-2s 不可接受）

### 2.6 自动测速批次隔离（v3 新增）
- runAutoLoop 每轮开始时记 `runStartIndex = records.length` + `run_id`（时间戳）
- records.push 带 `source:'auto' / type:'first'|'second' / round / run_id`
- 报告（renderAutoReport）只取 `records.slice(runStartIndex)`，不混入历史 auto 记录
- 删除中间一条记录会导致后续奇偶身份翻转（§2.3 依赖序号），所以删记录要谨慎

---

## 3. 代码风格

### 3.1 注释用中文
面向中文开发者，讲业务背景和踩坑经验。删注释 = 丢项目知识。

### 3.2 后端
- Python 3.10+
- 单文件 `server.py`（暂时不拆分，避免无意义的目录膨胀）
- typing + dataclass
- 错误处理：`AdbError` → HTTP 400，不要让前端 fetch 挂起

### 3.3 前端
- 单文件 `static/index.html`（HTML+JS 内嵌）
- 样式用 **Tailwind CSS**：开发期走 CDN（`<script src="https://cdn.tailwindcss.com">`），定稿后用 `npx tailwindcss -i input.css -o static.css --minify` 编译成离线 CSS，删除 CDN `<script>`，成品零网络依赖
- 不引入 JS 框架（React/Vue 等），保持原生 JS
- 所有交互元素的 id 保持稳定（JS 依赖），改样式不破坏 JS 契约

### 3.3.1 设计令牌（Linear，硬规则）
- **唯一色源**：[`static/themes/linear.json`](static/themes/linear.json)（Linear 暗色设计值，2026-08 全面重设计，替代原 OC-2 管线）
- **产物**：[`static/themes/linear.css`](static/themes/linear.css)（由 `_bake_linear.py` 从 palette + overrides 生成）；禁止手填业务色 hex
- 页面：`data-theme="linear"` + `data-color-scheme="dark"`（**仅暗色**；亮色切换元素 `#themeLightBtn` 保留但 CSS 隐藏）
- 兼容别名层（`--bg/--primary/--mint/--outline-var` 等）在 linear.css 内映射到 Linear 语义，页面统一消费这些别名
- 字体：`--font-ui` = Inter Variable（自托管 `static/fonts/`，OFL，零网络依赖）+ 系统回退；`--font-mono` 只管数据/日志；**UI 控件禁止 mono**
- 字阶：6 级 token（`--text-display/value/title/body/label/data`），字体内禁止 tokens 之外的裸 px 字号
- 改色流程：改 `linear.json` → 运行 `python static/themes/_bake_linear.py` → 硬刷新验证
- 强调色纪律：`--primary`（紫 #5e6ad2）唯一饱和色；状态色只以 ≤12% alpha 底纹 / 圆点出现
- 允许例外：透明黑白 `#000` / `#fff` 仅用于 `color-mix` / 描边对比

### 3.4 启动器
- `Start.bat` 用纯 ASCII（cmd 的 GBK 解析不支持 UTF-8 中文）
- 中文提示放 HTML/README 里（这些是 UTF-8）

---

## 4. 不要做的事

- ❌ 引入无意义的文件/文件夹（备份文件、临时测试脚本、多余的工具脚本）
- ❌ "顺便重构"—— 改 A 的 bug 时不要顺手"优化"无关的 B
- ❌ 新增依赖（pip 包）不讨论
- ❌ 改契约（API 字段、函数签名）不两边同步
- ❌ 在错误的状态上叠加修复—— 发现自己改错了，**回滚到上一个已知正确状态**，不要层层打补丁

---

## 5. 验证清单

提交前必跑：
- [ ] `python -c "import ast; ast.parse(open('server.py', encoding='utf-8').read())"` 通过
- [ ] 后端能启动：`uvicorn server:app --port 8766`
- [ ] `/api/health` 返回 200
- [ ] `/api/cold_start` 返回的字段与前端 `startTimer` 读取的字段一致
- [ ] 核心逻辑（计时/坐标/统计）改动后用 mock 数据验证过

---

## 6. 历史教训

**教训一：v1 → v2 重写时引入 -56 年 bug（2026-06 第一轮）**
v1 的 `server.ps1` 计时是对的。v2 重写时引入了 `start_wall`（wall 时钟）+ `start_ts`（perf 时钟）
两个字段，前端用 `(serverNow - startWall) * 1000` 把 perf 值（小数）和 epoch 秒（大数）混在一起减，
导致计时变成 -56 年的荒谬数字。

**教训二：wall 校准公式让计时器起跳显示 0.6s（2026-06 第二轮）**
-56 年 bug 修好后，v2 用 `perfAtTap = performance.now() - (Date.now() - start_wall*1000)` 做校准。
看起来"更精确"，但实测起跳显示 0.6s 而不是 0.0s。根因：`Date.now() - start_wall*1000` 这段
**不只是网络延迟**，还包含了服务端 `tap_norm/launch_pkg` 命令本身的执行时间（adb fork + input/monkey
启动，实测 475-832ms）。把这个当成"网络延迟"去减，等于把 tap 执行时间误当网络延迟，
导致 `perfAtTap` 被推到 tap 之前 → 起跳就是 0.6s。

**最终处理**：放弃 wall 校准，前端计时回滚到 v1 的纯 `performance.now()` 方案（响应回来后直接打点）。
v1 漏掉了 tap 执行时间（~200ms，偏小但每次都偏），wall 校准反而多算了（~600ms 偏大）。
两端都不准，但 v1 简单、起跳 0.0s、体感正常。真正的精度提升要靠 OCR 自动停表（终点客观化），
不是靠起点校准。

**根因（两次教训同源）**：重写时没读懂 v1 的工作原理，引入了"看起来更专业"的多字段校准，
没有用真实数据验证就交付。

**教训**：
1. 核心逻辑要简单到一眼能看懂，复杂换算就是 bug 温床
2. 重写 ≠ 改进，必须先证明原版有问题
3. 改完必须用真实数据验证，不能凭"应该对"
4. "更精确"的校准公式如果不验证，可能比原版偏得更大

---

**教训三：在错误的状态上叠加"可信度增强"（2026-07 自动测速迭代）**
用户要求"卸装安装 log 加日期时间"，agent 逐步叠加了 MD5 指纹 → 时间戳 → capture_err
三重处理，把 13 行的 `reinstall` 弄成 50 行怪兽。每层单独看"有道理"，叠在一起就成了灾难。
用户原话："明明很简单的卸载和安装，现在被你弄得很不透明和可信"。
最终回退到 13 行原始版（只留 adb 原始输出 + 前端 autoLog 加日期）。

**教训四：文字 OCR 停表精度不可接受（2026-07 自动测速迭代）**
为"自动判定启动成功"，先做了 OCR 全图文字匹配停表。实测 RapidOCR 全图推理
1373ms/次（1080×2400），停表精度 ±1-2s。用户判断"1s 误差太大，直接放弃"。
改用 cv2.matchTemplate 区域模板比对，matchTemplate 部分 3ms（区域搜索比全图快 49 倍）。
**结论**：神经网络推理（OCR）本质比像素比对（模板）重几十倍，自动化停表首选模板比对。

**教训五：纯色模板会让 matchTemplate 误命中（2026-07，第三方审核发现）**
cv2.matchTemplate 用 TM_CCOEFF_NORMED 时，纯色模板会返回 1.0 满置信度（实测确认）。
用户若点到画面空白处设模板，启动瞬间立即误命中停表，数据全错。
**修复**：set_marker_template 存模板前算灰度标准差，<15 拒绝。
**结论**：matchTemplate 不检查模板纹理，纯色/低方差模板必须在上游拒绝。

**教训六：路径穿越是 catch-all 路由的标配漏洞（2026-07，第三方审核发现）**
`@app.get("/{path:path}")` 直接拼接 `STATIC_DIR / path` 未验证边界，
`GET /..%2Fserver.py` 可读取完整源码。修复：resolve 后用 `relative_to(STATIC_DIR)` 验证。
**结论**：任何手写静态文件路由都必须做边界检查，或直接用框架的 StaticFiles。

**教训七：adb check=False 会吞掉失败（2026-07，第三方审核发现）**
`run(check=False)` 无视非零退出码。device offline 时 uninstall 输出为空，
代码继续执行 install -r，覆盖安装被当"干净重装"，污染首次冷启动样本。
**修复**：reinstall 里要求 adb 输出必须有明确 Success/Failure，空输出立即抛错。
**结论**：check=False 只用于"失败可接受"的场景（如 force_stop 杀不存在的进程），
关键操作（uninstall/install）必须验证输出。

**根因（教训三~七同源）**：
- 教训三：在错误状态上叠加修复，没有回滚到已知正确状态（违反本文件 §4）
- 教训四~七：没有用真实数据/边界用例验证就交付（违反本文件 §1.4）

**本次迭代新增的硬规则（agent 必须遵守）**：
1. **改完代码必须重启后端 + 浏览器验证 + 保持后端常驻**（不能只改本地就让用户访问不了）
2. **不擅自加"可信度增强"**（MD5/时间戳/指纹等），除非用户明确要
3. **自动停表只用模板比对**（cv2.matchTemplate），不用 OCR 全图文字匹配
4. **设模板必须拒绝纯色/低方差区域**（灰度标准差 < 15）
5. **第三方审核/代码审查发现的问题，先实测验证再修**（不盲从也不辩护）
