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
 * server.py 零改动：它通过 ROOT / __file__ 自动定位 adb/ 和 static/，
 * 我们只需把 cwd 设为项目根目录、--app-dir 指向项目根即可。
 */

'use strict';

const { spawn, execSync, execFileSync, spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const http = require('http');
const net = require('net');

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
  }

  // ── 路径计算 ──────────────────────────────────────────────

  /** .venv 目录绝对路径 */
  get venvDir() {
    return path.join(ROOT, '.venv');
  }

  /** .venv 内的 python 可执行文件路径 */
  get venvPython() {
    return this.isWin
      ? path.join(this.venvDir, 'Scripts', 'python.exe')
      : path.join(this.venvDir, 'bin', 'python');
  }

  /** 系统 Python 命令名（venv 不存在时用） */
  get systemPython() {
    return this.isWin ? 'python' : 'python3';
  }

  /** 优先用 venv Python，找不到回退系统 Python */
  get pythonPath() {
    return fs.existsSync(this.venvPython) ? this.venvPython : this.systemPython;
  }

  /** requirements.txt 路径 */
  get requirementsPath() {
    return path.join(ROOT, 'requirements.txt');
  }

  // ── 环境检测 ──────────────────────────────────────────────

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
    // 1. 确保环境就绪
    const envOk = await this.ensureEnvironment(onLog);
    if (!envOk) return false;

    // 2. 如果后端已在运行（比如 Start.bat 先启动了），直接复用
    onLog('info', '检查后端是否已运行...');
    if (await this.checkHealth()) {
      onLog('info', '后端已运行（检测到 /api/health 响应），直接复用。');
      return true;
    }

    // 3. 启动 uvicorn 子进程
    const pyExe = this.pythonPath;
    const args = [
      '-m', 'uvicorn', 'server:app',
      '--host', HOST,
      '--port', String(PORT),
      '--app-dir', ROOT,
    ];

    onLog('info', `启动后端: ${pyExe} ${args.join(' ')}`);

    try {
      this.process = spawn(pyExe, args, {
        cwd: ROOT,
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
        // 确保子进程继承正确的 PATH（ADB 可能需要）
        env: { ...process.env },
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

      // 如果子进程意外退出，不用再等了
      if (this.process === null && i > 2) {
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
   * macOS:   SIGTERM 足够；3 秒后仍不退出则 SIGKILL。
   */
  stop() {
    if (!this.process) return;

    const pid = this.process.pid;

    if (this.isWin) {
      // taskkill /T 递归杀子进程树，/F 强制终止（argv 数组，无 shell 注入）
      try {
        execFileSync('taskkill', ['/pid', String(pid), '/T', '/F'], { stdio: 'ignore' });
      } catch {
        // 进程可能已退出，忽略
      }
    } else {
      // Unix: 先 SIGTERM，给 uvicorn 优雅关闭的机会
      try {
        this.process.kill('SIGTERM');
      } catch {
        // 忽略
      }

      // 3 秒后仍存活则 SIGKILL
      const proc = this.process;
      setTimeout(() => {
        try {
          if (!proc.killed) {
            proc.kill('SIGKILL');
          }
        } catch {
          // 忽略
        }
      }, 3_000);
    }

    this.process = null;
  }
}

module.exports = { PythonManager, ROOT, HOST, PORT };
