/**
 * Python 后端生命周期管理器。
 *
 * 职责（对齐 Start.bat 的逻辑，Electron 版）：
 *   1. 检测 / 创建 Python 虚拟环境（.venv）
 *   2. 首次运行自动安装依赖（FastAPI / uvicorn / RapidOCR / OpenCV）
 *   3. 启动 uvicorn 子进程，加载 server.py
 *   4. 健康检查轮询 /api/health，确认就绪后通知主进程创建窗口
 *   5. 应用退出时优雅关闭 Python 进程（Windows 用 taskkill /T 杀进程树）
 *
 * 跨平台路径差异：
 *   Windows: .venv\Scripts\python.exe
 *   macOS:   .venv/bin/python
 *
 * server.py 零改动：它通过 ROOT / __file__ 自动定位 adb/ 和 static/。
 * cwd 与 --app-dir 指向 backendRoot（开发=项目根，打包=resources/backend）。
 */

'use strict';

const { spawn, execSync, execFileSync, spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const http = require('http');
const net = require('net');
// app.getPath('userData') 用于注入后端可写持久化目录（打包后 asar 只读）
const { app } = require('electron');

// 项目根目录（electron/ 的上一级）
const ROOT = path.resolve(__dirname, '..');

// 后端监听地址（仅本机，安全）
const HOST = '127.0.0.1';
const PORT = 8766;

// 健康检查参数
const HEALTH_MAX_RETRIES = 60;
const HEALTH_INTERVAL_MS = 1000;

// venv 安装参数
const VENV_TIMEOUT_MS = 120_000;    // 创建 venv 超时
const PIP_TIMEOUT_MS = 600_000;     // 装 RapidOCR + OpenCV 可能很慢


class PythonManager {
  constructor() {
    /** @type {import('child_process').ChildProcess | null} */
    this.process = null;
    this.isWin = process.platform === 'win32';
    this.isMac = process.platform === 'darwin';
    /** 主动停止标记：stop() 设 true，exit 事件据此区分意外退出 */
    this._stopping = false;
    /** 意外退出回调（主进程注册，用于弹窗恢复或退出） */
    this.onUnexpectedExit = null;
  }

  // ── 路径计算 ──────────────────────────────────────────────

  /**
   * 后端根目录。
   * 开发模式：项目根（electron/ 上一级），server.py/adb/static 都在源码树。
   * 打包模式：resources/backend（extraResources 解包目标），asar 外可读可写。
   * server.py 内部 ROOT = __file__ 所在目录，与此一致，无需改动。
   */
  get backendRoot() {
    return app.isPackaged
      ? path.join(process.resourcesPath, 'backend')
      : ROOT;
  }

  /** .venv 目录绝对路径 */
  get venvDir() {
    return path.join(this.backendRoot, '.venv');
  }

  /** .venv 内的 python 可执行文件路径 */
  get venvPython() {
    return this.isWin
      ? path.join(this.venvDir, 'Scripts', 'python.exe')
      : path.join(this.venvDir, 'bin', 'python');
  }

  /** 系统 Python 命令名（venv 不存在时用）；启动脚本可注入绝对路径 */
  get systemPython() {
    return process.env.CST_PYTHON || (this.isWin ? 'python' : 'python3');
  }

  /**
   * 内置嵌入式 Python（打包后 resources/python-embed/python.exe）。
   * 如果存在，优先使用——无需用户装 Python、无需 pip install、无需联网。
   * 开发模式下指向项目根的 python-embed/（由 scripts/build-python-embed.py 构建）。
   */
  get bundledPython() {
    const embedDir = app.isPackaged
      ? path.join(process.resourcesPath, 'python-embed')
      : path.join(ROOT, 'python-embed');
    const exe = this.isWin
      ? path.join(embedDir, 'python.exe')
      : path.join(embedDir, 'bin', 'python');
    return fs.existsSync(exe) ? exe : null;
  }

  /**
   * Python 可执行文件优先级：
   *   1. 内置嵌入式 Python（打包版，零依赖）
   *   2. .venv 虚拟环境 Python（开发 / Start.bat 模式）
   *   3. 系统 Python（首次启动时创建 venv）
   */
  get pythonPath() {
    if (this.bundledPython) return this.bundledPython;
    return fs.existsSync(this.venvPython) ? this.venvPython : this.systemPython;
  }

  /** requirements.txt 路径 */
  get requirementsPath() {
    return path.join(this.backendRoot, 'requirements.txt');
  }

  // ── 环境检测 ──────────────────────────────────────────────

  /** adb 是否可从项目目录或当前 PATH 使用（设备监听的启动前检查） */
  checkAdbAvailable() {
    const adbName = this.isWin ? 'adb.exe' : 'adb';
    const bundled = path.join(this.backendRoot, 'adb', adbName);
    if (fs.existsSync(bundled)) return true;
    try {
      const result = spawnSync(adbName, ['version'], { stdio: 'ignore', timeout: 5_000 });
      return result.status === 0;
    } catch {
      return false;
    }
  }

  /** venv 是否已存在且可用 */
  venvExists() {
    return fs.existsSync(this.venvPython);
  }

  /**
   * 检查系统是否装了 Python（任意版本）。
   * 用 spawnSync + argv 数组（非 shell 字符串），避免注入风险。
   */
  checkSystemPython() {
    try {
      const result = spawnSync(this.systemPython, ['--version'], {
        stdio: 'pipe',
        timeout: 5_000,
      });
      return result.status === 0 || result.status === null;
    } catch {
      return false;
    }
  }

  /**
   * 创建虚拟环境 + 安装依赖（首次启动时自动执行）。
   * 逻辑与 Start.bat 完全一致，只是换成了 Node.js 实现。
   *
   * @param {(level: string, msg: string) => void} onLog - 日志回调
   * @returns {Promise<boolean>} 成功返回 true
   */
  async ensureEnvironment(onLog) {
    if (this.venvExists()) {
      onLog('info', '检测到已有虚拟环境 (.venv)');
      return true;
    }

    if (!this.checkSystemPython()) {
      onLog('error', '未找到 Python。请安装 Python 3.10+ 并确保已添加到系统 PATH。');
      onLog('error', '下载地址: https://www.python.org/downloads/');
      return false;
    }

    // ── 创建 venv（execFileSync + argv 数组，无 shell 注入风险）──
    onLog('info', '首次启动：正在创建 Python 虚拟环境...');
    try {
      execFileSync(this.systemPython, ['-m', 'venv', this.venvDir], {
        stdio: 'pipe',
        timeout: VENV_TIMEOUT_MS,
      });
    } catch (e) {
      onLog('error', `创建虚拟环境失败：${e.message}`);
      return false;
    }
    onLog('info', '虚拟环境创建完成。');

    // ── 升级 pip ──
    onLog('info', '升级 pip...');
    try {
      execFileSync(this.venvPython, ['-m', 'pip', 'install', '--upgrade', 'pip'], {
        stdio: 'pipe',
        timeout: PIP_TIMEOUT_MS,
      });
    } catch {
      onLog('warn', 'pip 升级失败（非致命，继续安装依赖）');
    }

    // ── 安装依赖 ──
    onLog('info', '安装依赖（FastAPI / uvicorn / RapidOCR / OpenCV），约需 1-3 分钟...');
    onLog('info', '首次会下载 RapidOCR ONNX 模型（~20MB），请耐心等待。');
    try {
      execFileSync(
        this.venvPython,
        ['-m', 'pip', 'install', '-r', this.requirementsPath],
        { stdio: 'pipe', timeout: PIP_TIMEOUT_MS }
      );
      onLog('info', '依赖安装完成。');
    } catch (e) {
      onLog('error', '依赖安装失败。');
      onLog('error', `可手动执行: "${this.venvPython}" -m pip install -r requirements.txt`);
      if (e.stderr) {
        const stderr = e.stderr.toString().split('\n').filter(Boolean).slice(-5);
        stderr.forEach(line => onLog('error', `  ${line}`));
      }
      return false;
    }

    return true;
  }

  // ── 进程启动 ──────────────────────────────────────────────

  /**
   * 启动 Python 后端子进程（uvicorn + server.py）。
   *
   * 启动命令与 Start.bat 一致：
   *   python -m uvicorn server:app --host 127.0.0.1 --port 8766 --app-dir <ROOT>
   *
   * @param {(level: string, msg: string) => void} onLog
   * @returns {Promise<boolean>} 后端就绪返回 true
   */
  async start(onLog) {
    // 重置主动停止标记（新一轮启动，exit 视为意外）
    this._stopping = false;

    // ── 内置 Python 模式：跳过 venv / pip，直接启动 ──
    if (this.bundledPython) {
      onLog('info', '使用内置 Python 运行时（无需安装 Python / pip install）');
    } else {
      // 1. 确保环境就绪（开发模式 / Start.bat 模式走 venv）
      const envOk = await this.ensureEnvironment(onLog);
      if (!envOk) return false;
    }

    // 2. 如果后端已在运行（比如 Start.bat 先启动了），直接复用
    onLog('info', '检查后端是否已运行...');
    if (await this.checkHealth()) {
      onLog('info', '后端已运行（检测到 /api/health 响应），直接复用。');
      onLog('warn', '注意：复用的后端可能由 Start.bat 启动（监听 0.0.0.0，局域网可访问）。如需仅本机访问，请先关闭 Start.bat 后端再启动本应用。');
      return true;
    }

    // 3. 运行时源码保护（Mac 等非内嵌 Python 平台）
    //    构建时保留的 server.py，在此用「实际运行的 Python」编译为 .pyc 后删除源码。
    //    保证字节码 magic number 与运行时 Python 完全匹配（Windows 内嵌 Python
    //    已在构建时编译过，.pyc 存在则跳过）。
    this.ensureSourcelessBackend(onLog);

    // 4. 启动 uvicorn 子进程
    const pyExe = this.pythonPath;
    const args = [
      '-m', 'uvicorn', 'server:app',
      '--host', HOST,
      '--port', String(PORT),
      '--app-dir', this.backendRoot,
    ];

    onLog('info', `启动后端: ${pyExe} ${args.join(' ')}`);

    // 打包后 ROOT 落在 asar 只读区，projects/ 无法 mkdir；
    // 通过 CST_PROJECTS_DIR 注入 userData/projects（server.py 优先读它）。
    // 开发模式不注入 → server.py 回退 ROOT/projects（仓库根，零回归）。
    const projectsDir = app.isPackaged
      ? path.join(app.getPath('userData'), 'projects')
      : null;

    try {
      this.process = spawn(pyExe, args, {
        cwd: this.backendRoot,
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
        // 确保子进程继承正确的 PATH（ADB 可能需要）
        env: { ...process.env, ...(projectsDir ? { CST_PROJECTS_DIR: projectsDir } : {}) },
      });
    } catch (e) {
      onLog('error', `后端启动失败：${e.message}`);
      return false;
    }

    // 4. 转发 stdout/stderr 到日志
    this.process.stdout?.on('data', (data) => {
      const text = data.toString().trim();
      if (text) {
        text.split('\n').forEach(line => onLog('py-out', line));
      }
    });

    this.process.stderr?.on('data', (data) => {
      const text = data.toString().trim();
      if (text) {
        text.split('\n').forEach(line => onLog('py-err', line));
      }
    });

    this.process.on('exit', (code, signal) => {
      const reason = signal ? `signal=${signal}` : `code=${code}`;
      onLog('info', `后端进程已退出 (${reason})`);
      this.process = null;
      // 非主动停止 → 意外退出，通知主进程（弹窗恢复或退出）
      if (!this._stopping && this.onUnexpectedExit) {
        this.onUnexpectedExit(reason);
      }
    });

    this.process.on('error', (err) => {
      onLog('error', `后端进程错误：${err.message}`);
    });

    // 5. 等待健康检查通过
    onLog('info', `等待后端就绪（最多 ${HEALTH_MAX_RETRIES} 秒）...`);

    // RapidOCR 首次初始化约 2 秒，加上 uvicorn 启动，通常 3-5 秒就绪
    const ready = await this.waitForHealth(HEALTH_MAX_RETRIES, HEALTH_INTERVAL_MS, onLog);

    if (ready) {
      onLog('info', '后端就绪。');
    } else {
      onLog('error', `后端在 ${HEALTH_MAX_RETRIES} 秒内未就绪。`);
      onLog('error', '可能原因：依赖未装全、端口被占用、Python 版本不兼容。');
      this.stop();  // 清理半启动的进程
    }

    return ready;
  }

  // ── 运行时源码保护 ────────────────────────────────────────

  /**
   * 运行时编译 server.py → server.pyc（仅当 server.py 仍存在时）。
   *
   * 背景：Windows 打包时用内嵌 Python（3.11.9 固定）编译，运行时同一版本，安全。
   * Mac 用户 Python 版本不可控，构建时编译的 .pyc 可能 magic number 不匹配
   * （bad magic number → uvicorn 导入失败 → App 打不开）。
   * 因此 Mac 包保留 server.py 源码，首次启动时用实际运行的 Python 编译：
   *   1. 编译 server.py → server.pyc（平铺格式，uvicorn 可直接导入）
   *   2. 删除 server.py，只留字节码
   *
   * @param {(level: string, msg: string) => void} onLog
   */
  ensureSourcelessBackend(onLog) {
    // 仅打包模式生效；开发模式保留源码（开发者需要改代码）
    if (!app.isPackaged) return;

    const serverPy = path.join(this.backendRoot, 'server.py');
    if (!fs.existsSync(serverPy)) {
      return;  // 已保护（Windows 构建时已编译）
    }

    const pyExe = this.pythonPath;
    onLog('info', '检测到 server.py 源码，进行运行时编译保护...');

    try {
      // py_compile.compile(源文件, 平铺 .pyc 输出) —— 与编译者 magic 完全一致
      const cmd = `import py_compile; py_compile.compile(${JSON.stringify(serverPy)}, ${JSON.stringify(serverPy + 'c')}, doraise=True)`;
      execFileSync(pyExe, ['-c', cmd], {
        cwd: this.backendRoot,
        timeout: 30_000,
      });
    } catch (e) {
      onLog('warn', `运行时编译失败，保留 server.py（${e.message}）`);
      return;
    }

    const serverPyc = path.join(this.backendRoot, 'server.pyc');
    if (fs.existsSync(serverPyc)) {
      try {
        fs.unlinkSync(serverPy);
        onLog('info', '源码保护完成：server.py → server.pyc');
      } catch {
        onLog('warn', 'server.py 删除失败（可能被占用），保留源码');
      }
    }
  }

  // ── 健康检查 ──────────────────────────────────────────────

  /**
   * 单次健康检查：GET /api/health。
   * @returns {Promise<boolean>}
   */
  checkHealth() {
    return new Promise((resolve) => {
      const req = http.get(
        `http://${HOST}:${PORT}/api/health`,
        { timeout: 2_000 },
        (res) => {
          res.resume();
          resolve(res.statusCode === 200);
        }
      );
      req.on('error', () => resolve(false));
      req.on('timeout', () => {
        req.destroy();
        resolve(false);
      });
    });
  }

  /**
   * 轮询等待后端就绪。
   * @param {number} maxRetries - 最大重试次数
   * @param {number} intervalMs - 每次间隔（毫秒）
   * @param {(level: string, msg: string) => void} onLog
   * @returns {Promise<boolean>}
   */
  async waitForHealth(maxRetries, intervalMs, onLog) {
    let lastProgressAt = 0;

    for (let i = 0; i < maxRetries; i++) {
      if (await this.checkHealth()) {
        return true;
      }

      // 每 5 秒输出一次进度，避免日志太多
      const now = Date.now();
      if (now - lastProgressAt > 5_000) {
        onLog('info', `  等待中... (${i + 1}/${maxRetries})`);
        lastProgressAt = now;
      }

      // 如果子进程意外退出，不用再等了（i>0：首轮之后即可判定，避免前 3 秒白等）
      if (this.process === null && i > 0) {
        onLog('error', '后端进程已退出，停止等待。');
        return false;
      }

      await new Promise(r => setTimeout(r, intervalMs));
    }
    return false;
  }

  // ── 进程关闭 ──────────────────────────────────────────────

  /**
   * 优雅关闭 Python 后端。
   *
   * Windows: uvicorn 会 spawn 子进程（reloader），必须用 taskkill /T 杀整棵树。
   * Unix:    SIGTERM 优雅关闭，同步轮询 2 秒后 SIGKILL 兑底（不用 setTimeout，
   *          避免 app.quit 后事件循环已停导致僵尸进程）。
   */
  stop() {
    if (!this.process) return;
    // 标记主动停止，exit 事件据此不触发意外退出回调
    this._stopping = true;

    const pid = this.process.pid;

    if (this.isWin) {
      // taskkill /T 递归杀子进程树，/F 强制终止（argv 数组，无 shell 注入）
      try {
        execFileSync('taskkill', ['/pid', String(pid), '/T', '/F'], { stdio: 'ignore' });
      } catch {
        // 进程可能已退出，忽略
      }
    } else {
      // Unix: SIGTERM 优雅关闭，同步轮询最多 2 秒后 SIGKILL 兑底。
      // 不用 setTimeout：app.quit() 后事件循环可能已停，回调不执行会留僵尸进程。
      try {
        this.process.kill('SIGTERM');
      } catch {
        // 忽略
      }
      const deadline = Date.now() + 2_000;
      while (Date.now() < deadline) {
        try {
          process.kill(pid, 0);  // 採测进程是否存活（不发信号）
        } catch {
          break;  // 已退出
        }
        // 同步短睡 100ms 降 CPU（sleep 命令 Unix 通用；Windows 走上面 if 分支不会到这）
        try {
          spawnSync('sleep', ['0.1'], { stdio: 'ignore', timeout: 200 });
        } catch {
          break;
        }
      }
      // 超时仍未退出则 SIGKILL
      try {
        process.kill(pid, 'SIGKILL');
      } catch {
        // 忽略（已退出）
      }
    }

    this.process = null;
  }
}

module.exports = { PythonManager, ROOT, HOST, PORT };
