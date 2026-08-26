# 变更日志 — 2026-08-25（方案 C：全面治理）

## 背景

第三方审核报告确认 3 高 / 7 中 / 7 低共 17 项问题。用户选定方案 C（高+中+低全部处理）。
所有修改不触碰计时契约（AGENTS §2.1）、坐标换算、统计算法三条命脉。

## 高优先级修复

1. **iOS `/api/device_info` 必崩 AttributeError**（server.py）
   - `IosDevice` 此前无 `model` 属性，iOS 设备调用该端点必抛 500。
   - 新增 `IosDevice.model` 惰性缓存 property：lockdown 查 `DeviceName` →
     退 `ProductType` → 失败返回空串（符合「缺失为空」契约），失败结果缓存避免反复连设备。
   - 回归：`tests/test_audit_2026_08.py::TestIosModel`（失败/缓存/优先级/回退四路径）；
     实机验证 Android 路径不受影响（Pixel 6a 返回完整规格）。

2. **CSS 变量名拼错 + fallback 硬编码 hex 静默生效**（index.html 多设备日志列）
   - `var(--outline-variant, #333)` → `var(--outline-var)`；
     `var(--surface-container-lowest, #1a1a2e)` → `var(--sc-lowest)`（oc-2.css 实际定义名）。
   - 删除掩盖问题的 hex fallback，违反 §3.3.1 的兜底一并清除。

3. **JS 裸 hex 业务色**（index.html 并行设备模板就绪标记）
   - `'#22c55e'/'#ef4444'` → `var(--mint)`/`var(--danger)`，明暗主题自动跟随。

## 中优先级修复

4. **apk_path 文件系统探测归一化**（server.py reinstall / parse_apk）
   - 「文件不存在」不再回显用户传入的任意绝对路径；OSError 统一为
     「APK 文件不可用（不存在或无法读取）」，与存在性区分的探测通道封口。

5. **包名/bundle_id 白名单校验**（server.py `_check_pkg`）
   - `^[A-Za-z0-9._]+\Z` 全串校验，接入 launch_pkg / force_stop / reinstall /
     verify_launch / sys_baseline / cold_start 六个端点。
   - 封堵 adb shell 参数拼接给设备端 sh 的注入面（`;` `$()` 反引号 空格 换行等）。
   - reinstall 校验失败走 `{ok:false}` 既有通道而非 500。
   - 回归：TestCheckPkg 9 组注入样例 + 实机 curl 验证拒绝 `com.a; shutdown`。

6. **cold_start 的 reset_marker_watch 移入设备锁内**（server.py）
   - 原先在锁释放后执行，窗口期并发 check_auto 可能读到上一轮 streak/below 残留
     造成误停表或漏停表。现于锁内、tap/launch 之后执行（RLock 同线程可重入）。
   - 回归：TestColdStartResetOrdering 断言顺序 force_stop→tap→reset(owned=True)。

7. **upload_apk 大小上限 + ZIP 魔数校验**（server.py，常量模块级便于测试）
   - 上限 `APK_MAX_BYTES = 4GB` 流式累计、超限断流并删半成品；
   - 首块必须 `PK\x03\x04`/`PK\x05\x06`，改名 .apk 的任意文件直接 400 并清理。
   - 回归 + 实机 curl 双向验证（fake 头拒收、PK 头放行）。

8. **verify_launch / sys_baseline 忙碌提示通道**（server.py + index.html）
   - `DeviceSession.busy` 标志在长事务置位（finally 保证复位）；
   - `/api/devices` 对忙碌设备 additive 透出 `busy:true`（未忙碌不带键，契约向前兼容）；
   - 前端：设备下拉追加 `⏳忙` 后缀，并行设备芯片加 ⏳ 徽标（title 说明排队语义）。

9. **次要请求裸 fetch 迁移 apiFetch**（index.html 5 处）
   - pressKey / syncMarkerReadyFromServer / refreshProjectList /
     device_info(Word 导出内, 放宽 60s) / sys_baseline(专用 180s——最长 10 轮×冷却，
     默认 15s 会掐断本来成功的长事务)。后端卡死时按钮不再永久挂起。

10. **python-manager 重启竞态**（electron/python-manager.js）
    - exit 回调改为闭包捕获本次 spawn 的 child（代际引用）：只有退出的就是当前进程
      才清理引用/上报意外退出。修复「故障弹窗选重启后旧进程迟到 exit 再弹一次框」。

## 低优先级修复

11. scrcpy IPC 五处调用补 try/catch（mirror/record 按钮对 + initScrcpy），
    主进程异常不再变成 unhandled rejection + 按钮状态脱节。
12. Delete 快捷键删除样本前加 confirm（含序号提示）；防误触翻转奇偶分组身份（§2.3）。
13. set_marker / set_skip 约 40 行逐字重复的裁剪逻辑抽为 `_crop_template_region()`
    （尺寸换算/越界平移/空模板/纯色拒绝单点维护），行为与原版逐项一致（回归覆盖）。
14. shot_errors 双计数去重：路由层不再重复自增，`screenshot_bytes` 内部单一来源。
15. 版本号双写消除：关于对话框改读 `app.getVersion()`。
16. CSV 导出加 `csvCell()`：按需引号包裹（内部引号翻倍）+ `=+-@` 开头公式注入防护；
    安全值输出字节不变。

## 明确不做（附原因）

- **iOS 单例隧道拆分为按 UDID 多隧道**：pymobiledevice3 硬约束——PyTCP 栈是
  进程级单例（UserspaceTun 文档明文 one tunnel per process），第二个 aopen 直接抛
  "already active"。已在 `_ios_tunnel_state` 注释处补全源码依据，防止后续 agent
  把它当 bug 修出运行时崩溃。真并行 iOS 测速需子进程隔离，属架构级改动，收益/风险比不划算。
- Start-Web.bat 绑定 0.0.0.0 保持现状（脚本注释已声明是局域网使用的有意取舍）；
  本次通过 4/5 两项收窄了实际可利用面。

## 验证记录

- `ast.parse(server.py)` 通过
- pytest：90 passed（58 存量 + 32 新增 tests/test_audit_2026_08.py）
- node --test stats-core：7 passed；node --check electron/main.js、python-manager.js 通过
- index.html 内嵌 JS 提取后 node --check 通过
- 后端实机启动（127.0.0.1:8766）：/api/health 200；/api/devices 含真机且 busy 字段
  additive 正确；reinstall 包名注入实机拒绝；upload 魔数双向实机验证；
  device_info Android 真机返回 Pixel 6a 完整规格；IosDevice.model 无设备时返回 ''
- GET / 页面 200，主题 CSS/apiFetch/csvCell/busy 徽标标记齐全

## 已知遗留（未在本次范围）

- index.html 其余 ~15 处一次性 UI fetch 未迁移 apiFetch（主链路早已覆盖，
  属机械替换，建议下次迭代统一清扫）；
- verify_launch/sys_baseline 持锁期间同设备轮询排队是设计意图（串行化保正确性），
  busy 徽标已提供解释通道，未做请求级取消。
