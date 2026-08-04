/**
 * Electron 主进程。
 *
 * 职责：
 *   1. 单实例锁定（防止多开导致端口冲突）
 *   2. 启动 Python 后端子进程（uvicorn + server.py）
 *   3. 后端就绪后创建 BrowserWindow，加载 http://127.0.0.1:8766/
 *   4. 后端启动失败时显示错误窗口（含诊断日志）
 *   5. 应用退出时优雅关闭 Python 子进程
 *
 * 开发模式（npm run dev 或 --dev 参数）：
 *   - 自动打开 DevTools
 *   - Python 后端 stdout/stderr 转发到终端
 *
 * 安全配置：
 *   - contextIsolation: true  （渲染进程与 Node.js 隔离）
 *   - nodeIntegration: false  （渲染进程不直接访问 Node.js API）
 *   - preload 脚本通过 contextBridge 暴露最小 API
 */

'use strict';

const { app, BrowserWindow, shell, Menu, dialog, ipcMain, nativeImage } = require('electron');
const { execFileSync, spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const { PythonManager, HOST, PORT } = require('./python-manager');
const { ScrcpyManager } = require('./scrcpy-manager');

// ── 全局状态 ────────────────────────────────────────────────

const isDev = process.argv.includes('--dev') || !app.isPackaged;

/** @type {BrowserWindow | null} */
let mainWindow = null;

/** Python 后端管理器 */
const pyManager = new PythonManager();

/** 启动闪屏窗口（后端启动期间显示，避免用户看到空白等待）*/
let splashWindow = null;

function createSplashWindow() {
  splashWindow = new BrowserWindow({
    width: 480,
    height: 360,
    frame: false,
    resizable: false,
    transparent: true,
    alwaysOnTop: true,
    show: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  const html = `<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      font-family: "Microsoft YaHei", "PingFang SC", -apple-system, sans-serif;
      display: flex; flex-direction: column;
      align-items: center; justify-content: center;
      height: 100vh;
      background: rgba(26, 26, 46, 0.95);
      border-radius: 16px;
      border: 1px solid rgba(250, 178, 131, 0.2);
      color: #e0e0e0;
      -webkit-user-select: none;
      overflow: hidden;
    }
    .logo {
      font-size: 28px; font-weight: bold;
      margin-bottom: 24px;
      color: #fab283;
    }
    .spinner {
      width: 36px; height: 36px;
      border: 3px solid rgba(250, 178, 131, 0.2);
      border-top-color: #fab283;
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
      margin-bottom: 20px;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    #status {
      font-size: 14px; color: #ccc;
      text-align: center;
      min-height: 20px;
      max-width: 380px;
      word-break: break-all;
    }
    #detail {
      font-size: 11px; color: #777;
      margin-top: 10px;
      min-height: 16px;
      max-width: 380px;
      text-align: center;
      font-family: "SF Mono", "Cascadia Mono", Consolas, monospace;
    }
    .bar-bg {
      width: 280px; height: 4px;
      background: rgba(255, 255, 255, 0.08);
      border-radius: 2px;
      margin-top: 16px;
      overflow: hidden;
    }
    .bar-fill {
      height: 100%;
      background: linear-gradient(90deg, #fab283, #f59e0b);
      border-radius: 2px;
      width: 0%;
      transition: width 0.5s ease;
    }
  </style>
</head>
<body>
  <div class="logo">⏱️ App 冷启测速</div>
  <div class="spinner"></div>
  <div id="status">正在启动...</div>
  <div id="detail"></div>
  <div class="bar-bg"><div class="bar-fill" id="bar"></div></div>
  <script>
    function updateStatus(text) {
      document.getElementById('status').textContent = text;
    }
    function updateDetail(text) {
      document.getElementById('detail').textContent = text;
    }
    function updateBar(percent) {
      document.getElementById('bar').style.width = Math.min(100, Math.max(0, percent)) + '%';
    }
  </script>
</body>
</html>`;

  splashWindow.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html));
  splashWindow.on('closed', () => { splashWindow = null; });
}

/**
 * 更新闪屏状态文字（用户能看到当前在干什么）。
 * @param {string} status - 主状态文字
 * @param {string} [detail] - 详细信息（可选）
 * @param {number} [progress] - 进度百分比 0-100（可选）
 */
function updateSplash(status, detail, progress) {
  if (!splashWindow) return;
  const esc = (s) => String(s || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, ' ');
  try {
    splashWindow.webContents.executeJavaScript(`updateStatus('${esc(status)}')`);
    if (detail !== undefined) {
      splashWindow.webContents.executeJavaScript(`updateDetail('${esc(detail)}')`);
    }
    if (progress !== undefined) {
      splashWindow.webContents.executeJavaScript(`updateBar(${progress})`);
    }
  } catch { /* 闪屏可能已关闭 */ }
}

/** scrcpy 镜像/录屏管理器 */
const scrcpyManager = new ScrcpyManager((eventName, payload) => {
  mainWindow?.webContents.send(eventName, payload);
});

// ── 全局异常兜底（借鉴 XYLog Viewer logMainFault）──
// 未捕获的异常不能让 Electron 静默崩溃——写入 userData/main-error.log，
// 用户报 bug 时有日志可查。放在 app.whenReady() 之前以捕获最早期错误。
process.on('uncaughtException', (err) => logMainFault('uncaughtException', err));
process.on('unhandledRejection', (reason) => logMainFault('unhandledRejection', reason));

function logMainFault(kind, err) {
  const msg = `[${new Date().toISOString()}] ${kind}: ${err?.stack || err?.message || err}\n`;
  try { process.stderr.write(msg); } catch {}
  try {
    const logDir = app.getPath('userData');
    fs.appendFileSync(path.join(logDir, 'main-error.log'), msg);
  } catch {}
}

/** 启动日志（错误窗口展示用） */
const startupLogs = [];

// ── 应用图标 ────────────────────────────────────────────────
// 从 build/icon.png 加载（如存在），否则用空 nativeImage 占位。
// 生产打包时 electron-builder 从 build/icon.ico / icon.icns 设置 exe/dmg 图标。
function loadAppIcon() {
  const iconPath = path.join(__dirname, '..', 'build', 'icon.png');
  if (fs.existsSync(iconPath)) {
    return nativeImage.createFromPath(iconPath);
  }
  return nativeImage.createEmpty();
}

const appIcon = loadAppIcon();

// ── 日志收集 ────────────────────────────────────────────────

/**
 * 日志回调：同时输出到终端和内存缓冲。
 * @param {string} level - info / warn / error / py-out / py-err
 * @param {string} msg
 */
function log(level, msg) {
  const ts = new Date().toLocaleTimeString();
  const prefix = `[${ts}] [${level}]`;
  const line = `${prefix} ${msg}`;
  startupLogs.push(line);

  // 转发关键进度到闪屏（用户能看到当前在干什么）
  if (splashWindow) {
    if (level === 'info' || level === 'warn') {
      // 根据日志内容判断阶段
      if (msg.includes('虚拟环境') || msg.includes('venv')) {
        updateSplash('正在创建 Python 虚拟环境...', msg, 15);
      } else if (msg.includes('依赖') || msg.includes('pip')) {
        updateSplash('正在安装 Python 依赖...', msg, 30);
      } else if (msg.includes('OCR') || msg.includes('模型')) {
        updateSplash('正在下载 OCR 模型...', msg, 50);
      } else if (msg.includes('启动后端') || msg.includes('uvicorn')) {
        updateSplash('正在启动后端服务...', msg, 70);
      } else if (msg.includes('等待') || msg.includes('就绪')) {
        updateSplash('正在等待后端就绪...', msg, 85);
      } else if (msg.includes('内置 Python') || msg.includes('无需安装')) {
        updateSplash('正在初始化运行时...', msg, 60);
      } else {
        updateSplash(msg, '', undefined);
      }
    } else if (level === 'error') {
      updateSplash('启动遇到问题', msg, undefined);
    }
  }

  // py-out / py-err 是 Python 后端的输出，用不同前缀区分
  if (level === 'py-out') {
    console.log(`  [py] ${msg}`);
  } else if (level === 'py-err') {
    console.error(`  [py!] ${msg}`);
  } else {
    console.log(line);
  }
}

// ── 窗口创建 ────────────────────────────────────────────────

/**
 * 创建主窗口，加载 Python 后端提供的前端页面。
 *
 * 加载方式：loadURL('http://127.0.0.1:8766/')
 * 这样前端的 fetch('/api/...') 相对路径自然指向后端，零改动。
 * FastAPI 的 static_fallback 路由负责提供 index.html / static.css 等静态文件。
 */
function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    // 最小 1120x700：三栏布局需要至少 1120px 宽度
    // （左 260 + 右 340 + 中栏 min-width 360 + padding/split ≈ 1000px），
    // 700px 高度确保日志区域有至少 150px 可见空间。
    // 低于 1100px 宽时前端 CSS 切换为列布局（媒体查询）。
    minWidth: 1120,
    minHeight: 700,
    show: false,       // 等 ready-to-show 再显示，避免白屏闪烁
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      // 允许加载 localhost 内容
      webSecurity: true,
    },
    title: 'App 冷启测速 · Cold Start Profiler',
    // 使用系统默认标题栏样式（macOS 自动交通灯，Windows 标准）
    titleBarStyle: 'default',
    icon: appIcon,
  });

  // 加载后端前端页面
  const url = `http://${HOST}:${PORT}/`;
  log('info', `加载前端: ${url}`);
  // 先清 Chromium 磁盘缓存再加载：旧版本无 Cache-Control 时的启发式缓存
  // 会让升级后的用户仍看到旧版前端（实测过），每次启动强制拿最新页面。
  mainWindow.webContents.session.clearCache().then(() => {
    mainWindow.loadURL(url);
  });

  // 窗口准备好后显示（避免白屏）
  mainWindow.once('ready-to-show', () => {
    mainWindow?.show();
    if (isDev) {
      mainWindow?.webContents.openDevTools({ mode: 'detach' });
    }
  });

  // 拦截外部链接（如点击 http/https 链接时用系统浏览器打开，而不是在 Electron 内导航）
  mainWindow.webContents.setWindowOpenHandler(({ url: targetUrl }) => {
    if (targetUrl.startsWith('http://') || targetUrl.startsWith('https://')) {
      shell.openExternal(targetUrl);
      return { action: 'deny' };
    }
    return { action: 'allow' };
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  // ── 自定义右键菜单（替换浏览器默认的「查看源代码/检查元素」）──
  mainWindow.webContents.on('context-menu', (event, params) => {
    const hasText = params.selectionText && params.selectionText.length > 0;
    const isEditable = params.isEditable;
    const menu = Menu.buildFromTemplate([
      // 可编辑区域：显示剪切/粘贴/全选
      ...(isEditable ? [
        { label: '撤销', role: 'undo', enabled: params.editFlags.canUndo, accelerator: 'CmdOrCtrl+Z' },
        { label: '重做', role: 'redo', enabled: params.editFlags.canRedo, accelerator: 'CmdOrCtrl+Shift+Z' },
        { type: 'separator' },
        { label: '剪切', role: 'cut', enabled: params.editFlags.canCut, accelerator: 'CmdOrCtrl+X' },
        { label: '复制', role: 'copy', enabled: params.editFlags.canCopy, accelerator: 'CmdOrCtrl+C' },
        { label: '粘贴', role: 'paste', enabled: params.editFlags.canPaste, accelerator: 'CmdOrCtrl+V' },
        { type: 'separator' },
        { label: '全选', role: 'selectAll', enabled: params.editFlags.canSelectAll, accelerator: 'CmdOrCtrl+A' },
      ] : []),
      // 选中文本但不可编辑：只显示复制
      ...(!isEditable && hasText ? [
        { label: '复制', role: 'copy', accelerator: 'CmdOrCtrl+C' },
        { type: 'separator' },
      ] : []),
      // 始终显示的开发者工具（生产模式下也保留，方便排查）
      { type: 'separator' },
      { label: '重新加载页面', role: 'reload', accelerator: 'CmdOrCtrl+R' },
      ...(isDev ? [{ label: '开发者工具', role: 'toggleDevTools', accelerator: 'F12' }] : []),
    ]);
    menu.popup(mainWindow);
  });
}

// ── 应用菜单栏 ──────────────────────────────────────────────
// 替换 Electron 默认菜单（含无关的「View」「Window」等），
// 构建与本项目操作语义匹配的菜单结构。
function buildAppMenu() {
  const template = [
    // ── 应用菜单（macOS 第一个菜单是应用名）──
    ...(process.platform === 'darwin' ? [{
      label: app.name,
      submenu: [
        { role: 'about', label: '关于 App 冷启测速' },
        { type: 'separator' },
        { role: 'services' },
        { type: 'separator' },
        { role: 'hide', label: '隐藏 App 冷启测速' },
        { role: 'hideOthers' },
        { role: 'unhide' },
        { type: 'separator' },
        { role: 'quit', label: '退出 App 冷启测速' },
      ],
    }] : []),
    // ── 操作 ──
    {
      label: '操作',
      submenu: [
        {
          label: '截图',
          accelerator: 'CmdOrCtrl+S',
          click: () => sendMenuCommand('manual-shot'),
        },
        { type: 'separator' },
        {
          label: '卸载重装 APK',
          accelerator: 'CmdOrCtrl+Shift+U',
          click: () => sendMenuCommand('reinstall'),
        },
        {
          label: '杀进程',
          accelerator: 'CmdOrCtrl+Shift+K',
          click: () => sendMenuCommand('force-stop'),
        },
        {
          label: '回主页',
          accelerator: 'CmdOrCtrl+H',
          click: () => sendMenuCommand('go-home'),
        },
        { type: 'separator' },
        {
          label: '上传 APK…',
          click: () => sendMenuCommand('upload-apk'),
        },
        { type: 'separator' },
        {
          label: '📱 实时镜像 (scrcpy)',
          click: () => sendMenuCommand('scrcpy-mirror'),
        },
        {
          label: '⏺ 录屏',
          click: () => sendMenuCommand('scrcpy-record'),
        },
      ],
    },
    // ── 编辑（右键菜单的菜单栏版本，含标准文本编辑操作）──
    {
      label: '编辑',
      submenu: [
        { role: 'undo', label: '撤销' },
        { role: 'redo', label: '重做' },
        { type: 'separator' },
        { role: 'cut', label: '剪切' },
        { role: 'copy', label: '复制' },
        { role: 'paste', label: '粘贴' },
        { role: 'selectAll', label: '全选' },
      ],
    },
    // ── 视图 ──
    {
      label: '视图',
      submenu: [
        { role: 'reload', label: '重新加载页面' },
        { role: 'forceReload', label: '强制重新加载' },
        { type: 'separator' },
        { role: 'zoomIn', label: '放大' },
        { role: 'zoomOut', label: '缩小' },
        { role: 'resetZoom', label: '重置缩放' },
        { type: 'separator' },
        { role: 'togglefullscreen', label: '全屏' },
      ],
    },
    // ── 窗口 ──
    {
      label: '窗口',
      submenu: [
        { role: 'minimize', label: '最小化' },
        { role: 'close', label: '关闭窗口' },
      ],
    },
    // ── 帮助 ──
    {
      label: '帮助',
      submenu: [
        {
          label: '关于 App 冷启测速',
          click: () => showAboutDialog(),
        },
      ],
    },
  ];
  // 开发模式加入 DevTools
  if (isDev) {
    template.find(t => t.label === '视图').submenu.push(
      { type: 'separator' },
      { role: 'toggleDevTools', label: '开发者工具', accelerator: 'F12' }
    );
  }
  const menu = Menu.buildFromTemplate(template);
  Menu.setApplicationMenu(menu);
}

/** 菜单命令发送到渲染进程 */
function sendMenuCommand(cmd) {
  if (mainWindow) {
    mainWindow.webContents.send('menu-command', cmd);
  }
}

/** 原生关于对话框 */
function showAboutDialog() {
  const options = {
    type: 'info',
    title: '关于 App 冷启测速',
    message: 'App 冷启测速 · Cold Start Profiler',
    detail: [
      '版本: 2.0.0',
      `Electron: ${process.versions.electron}`,
      `Chrome: ${process.versions.chrome}`,
      `Node.js: ${process.versions.node}`,
      '',
      'Python FastAPI + RapidOCR + OpenCV',
      'Electron 桌面客户端',
      '',
      '作者: 田海顺',
    ].join('\n'),
    buttons: ['确定'],
    icon: appIcon,
  };
  dialog.showMessageBox(mainWindow, options);
}

/**
 * 创建错误窗口（后端启动失败时显示）。
 * 用 data URL 加载内嵌 HTML，不依赖后端服务。
 *
 * @param {string} title - 窗口标题
 * @param {string} detail - 详细错误信息（含启动日志）
 */
function createErrorWindow(title, detail) {
  const win = new BrowserWindow({
    width: 720,
    height: 500,
    resizable: true,
    title: title,
    show: true,
    // 安全配置与主窗口对齐：错误窗口只加载 data URL，
    // 但仍显式禁用 Node 集成 + 开启沙箱，避免配置漂移被未来误改
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  // 转义 HTML 特殊字符，防止日志中的 < > & 导致渲染异常
  const esc = (s) => s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>${esc(title)}</title>
  <style>
    body {
      font-family: "Microsoft YaHei", "PingFang SC", -apple-system, sans-serif;
      padding: 32px;
      background: #1a1a2e;
      color: #e0e0e0;
      line-height: 1.7;
    }
    h2 { color: #ff6b6b; margin: 0 0 16px 0; }
    .tips { color: #aaa; font-size: 14px; margin-top: 16px; }
    .tips li { margin: 4px 0; }
    pre {
      background: #111;
      color: #ccc;
      padding: 16px;
      border-radius: 8px;
      font-size: 12px;
      font-family: "Cascadia Mono", Consolas, monospace;
      white-space: pre-wrap;
      word-break: break-all;
      max-height: 250px;
      overflow-y: auto;
      border: 1px solid #333;
    }
  </style>
</head>
<body>
  <h2>${esc(title)}</h2>
  <pre>${esc(detail)}</pre>
  <div class="tips">
    <p>排查步骤：</p>
    <ul>
      <li>确认已安装 <b>Python 3.10+</b> 并添加到系统 PATH</li>
      <li>确认端口 <b>${PORT}</b> 未被占用（Hyper-V / WSL 可能保留端口范围）</li>
      <li>尝试手动启动后端：<code>python -m uvicorn server:app --port ${PORT}</code></li>
      <li>查看上方的启动日志定位具体错误</li>
    </ul>
    <p style="margin-top:20px; color: #666;">关闭此窗口退出应用。</p>
  </div>
</body>
</html>`;

  win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html));

  win.on('closed', () => {
    app.quit();
  });
}

// ── 设备热插拔监听（adb track-devices，借鉴 XYLog Viewer）──
// track-devices 是流式命令：先输出当前设备列表，之后设备插拔时实时输出变化。
// 我们只把它当"有变化"的触发器——不做复杂解析，检测到变化就 IPC 通知前端刷新。
// 前端收到通知后调 /api/devices 拉取最新列表（复用现有逻辑）。
let deviceTrackerProc = null;

function startDeviceTracker() {
  const adbPath = path.join(pyManager.backendRoot, 'adb', 'adb.exe');
  if (!fs.existsSync(adbPath)) return;

  let initialized = false;
  let buffer = '';

  try {
    deviceTrackerProc = spawn(adbPath, ['track-devices'], {
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
  } catch {
    return;  // 启动失败不影响主流程，前端轮询兜底
  }

  deviceTrackerProc.stdout?.on('data', (chunk) => {
    buffer += chunk.toString('utf8');
    // track-devices 输出以空行分隔快照
    const snapshots = buffer.split('\n\n');
    buffer = snapshots.pop();  // 最后一段可能不完整，留在 buffer
    for (const _snap of snapshots) {
      if (!initialized) {
        initialized = true;  // 首次输出是初始列表，不需要通知
        continue;
      }
      // 后续输出 = 设备变化（插入/拔出/状态改变）
      mainWindow?.webContents.send('devices:changed');
    }
  });

  deviceTrackerProc.on('exit', () => {
    deviceTrackerProc = null;
    // adb server 被 kill-server 关闭时 track-devices 会退出，不自动重启
    // 前端的轮询兜底会继续工作
  });

  deviceTrackerProc.on('error', () => {
    deviceTrackerProc = null;
  });
}

function disposeDeviceTracker() {
  if (deviceTrackerProc) {
    try { deviceTrackerProc.kill(); } catch {}
    deviceTrackerProc = null;
  }
}

/**
 * 后端意外退出处理：弹窗让用户选择重启后端或退出应用。
 * @param {string} reason - 退出原因（code=... / signal=...）
 */
async function handleBackendCrash(reason) {
  if (!mainWindow) {
    app.quit();
    return;
  }
  const result = await dialog.showMessageBox(mainWindow, {
    type: 'error',
    title: '后端已断开',
    message: 'Python 后端已意外退出，应用功能将不可用。',
    detail: `退出原因：${reason}\n\n是否重启后端？`,
    buttons: ['重启后端', '退出应用'],
    defaultId: 0,
    cancelId: 1,
    noLink: true,
    icon: appIcon,
  });
  if (result.response === 0) {
    log('info', '用户选择重启后端...');
    const started = await pyManager.start(log);
    if (started) {
      log('info', '后端重启成功，重载前端。');
      mainWindow?.webContents.reload();
    } else {
      log('error', '后端重启失败，退出应用。');
      app.quit();
    }
  } else {
    app.quit();
  }
}

// ── 应用生命周期 ────────────────────────────────────────────

// 单实例锁定：防止多开（第二次打开时聚焦已有窗口）
const gotLock = app.requestSingleInstanceLock();

if (!gotLock) {
  // 已有实例在运行，直接退出
  app.quit();
} else {
  app.on('second-instance', () => {
    // 有人尝试打开第二个实例，聚焦已有窗口
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    log('info', `Electron ${process.versions.electron} (Chrome ${process.versions.chrome})`);
    log('info', `平台: ${process.platform} ${process.arch}`);
    log('info', `开发模式: ${isDev ? '是 (--dev)' : '否'}`);
    log('info', '────────────────────────────────');

    // 立即显示启动闪屏（用户不用盯着空白等待）
    createSplashWindow();

    log('info', '正在启动 Python 后端...');

    // 启动 Python 后端
    const started = await pyManager.start(log);

    if (!started) {
      log('error', '后端启动失败，显示错误窗口。');
      splashWindow?.close();
      const detail = startupLogs.join('\n');
      createErrorWindow('后端启动失败', detail);
      return;
    }

    log('info', '后端就绪，创建应用窗口。');

    // 闪屏显示完成状态后关闭
    updateSplash('启动完成！', '', 100);
    await new Promise(r => setTimeout(r, 500)); // 让用户看到 100%
    splashWindow?.close();

    // 后端意外退出时：先通知前端展示 overlay，再弹窗恢复（见 handleBackendCrash）
    pyManager.onUnexpectedExit = (reason) => {
      mainWindow?.webContents.send('backend-status', 'offline');
      handleBackendCrash(reason);
    };

    buildAppMenu();
    createMainWindow();

    // 后端就绪后启动设备热插拔监听（adb track-devices 流式命令）
    startDeviceTracker();
  });

  // 所有窗口关闭时退出（macOS 除外，但本工具不适合常驻菜单栏）
  app.on('window-all-closed', () => {
    app.quit();
  });

  // 应用即将退出时清理 Python 后端（统一在此清理，避免与 window-all-closed 重复调用）
  app.on('before-quit', () => {
    disposeDeviceTracker();
    scrcpyManager.dispose();
    pyManager.stop();
    // 兜底：确保 adb daemon 被关闭（后端被 taskkill /F 时 lifespan shutdown 不会执行。
    // 借鉴 XYLog Viewer AdbManager._killServerSync，防止 adb.exe 残留导致升级文件锁）
    const adbPath = path.join(pyManager.backendRoot, 'adb', 'adb.exe');
    try { execFileSync(adbPath, ['kill-server'], { timeout: 3000, windowsHide: true }); } catch {}
  });
}

// ── IPC 处理（主进程侧）─────────────────────────────────────

/**
 * 原生文件选择对话框：替代 HTML <input type="file">。
 *
 * 优势：系统原生外观、记住上次目录、过滤文件类型、跨平台行为一致。
 * 渲染进程通过 preload 暴露的 electronAPI.openFileDialog() 调用。
 */
ipcMain.handle('dialog:openFile', async (event, options) => {
  if (!mainWindow) return { canceled: true };

  // 安全校验：options 来自渲染进程（contextIsolation 已隔离 Node，
  // 但若前端被 XSS 仍可构造请求）。在此做白名单收敛：
  //   - title 强制 String + 截断，防超长/异常类型
  //   - filters 校验结构（数组 + 每项 {name, extensions[]}），extensions
  //     元素仅允许字母/数字/星号，否则回退默认
  //   - defaultPath 不透传：消除路径探测面（前端当前未使用，
  //     系统会自动记住上次打开目录）
  const safeTitle = typeof options?.title === 'string'
    ? options.title.slice(0, 100)
    : '选择文件';
  let safeFilters = [{ name: '所有文件', extensions: ['*'] }];
  if (Array.isArray(options?.filters)) {
    const valid = options.filters.filter(f =>
      f && typeof f.name === 'string' &&
      Array.isArray(f.extensions) &&
      f.extensions.every(e => typeof e === 'string' && /^[a-z0-9*]+$/i.test(e))
    );
    if (valid.length > 0) safeFilters = valid;
  }

  const result = await dialog.showOpenDialog(mainWindow, {
    title: safeTitle,
    filters: safeFilters,
    properties: ['openFile'],
  });
  if (result.canceled || result.filePaths.length === 0) {
    return { canceled: true };
  }
  return { canceled: false, filePath: result.filePaths[0] };
});

/**
 * 原生消息对话框：替代 alert() / confirm()。
 *
 * type: 'none' | 'info' | 'error' | 'question' | 'warning'
 * buttons: 按钮文字数组
 * 返回: { response: 按钮索引, checkboxChecked: bool }
 */
ipcMain.handle('dialog:showMessage', async (event, options) => {
  if (!mainWindow) return { response: 0 };
  const result = await dialog.showMessageBox(mainWindow, {
    type: options.type || 'info',
    title: options.title || 'App 冷启测速',
    message: options.message || '',
    detail: options.detail || '',
    buttons: options.buttons || ['确定'],
    defaultId: options.defaultId ?? 0,
    cancelId: options.cancelId ?? -1,
    noLink: true,
  });
  return { response: result.response, checkboxChecked: result.checkboxChecked };
});

// ── scrcpy 镜像 / 录屏 IPC ──────────────────────────────────

ipcMain.handle('scrcpy:mirror:start', async (event, { serial, model }) => {
  return scrcpyManager.startMirror(serial, model);
});

ipcMain.handle('scrcpy:mirror:stop', async () => {
  return scrcpyManager.stopMirror();
});

ipcMain.handle('scrcpy:record:start', async (event, { serial }) => {
  return scrcpyManager.startRecord(serial);
});

ipcMain.handle('scrcpy:record:stop', async () => {
  return scrcpyManager.stopRecord();
});

ipcMain.handle('scrcpy:getStatus', async () => {
  return scrcpyManager.getStatus();
});
