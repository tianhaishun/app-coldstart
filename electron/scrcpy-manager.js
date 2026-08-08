/**
 * scrcpy 镜像 / 录屏管理器。
 *
 * 与后端同一 Electron + 内置 adb 技术栈：
 *   - 镜像：scrcpy -s <serial> --always-on-top --window-title ...
 *     → 弹独立 SDL 窗口，始终置顶，不嵌入 Electron（零渲染开销）
 *   - 录屏：scrcpy -s <serial> --no-window --no-playback --record <file>
 *     → 后台无窗口录制，镜像运行中可随时开始/停止
 *   - 环境变量 ADB=<内置adb> + SCRCPY_SERVER_PATH=<内置server>
 *     → 复用后端同一份 adb-server，互不冲突
 *
 * 与后端截图轮询的关系：
 *   - scrcpy 走独立视频流（adb forward），不执行 adb 命令，不抢 SESSION._lock
 *   - 自动测速时的截图轮询（screenshot_bgr）和模板比对不受影响
 *   - 镜像是"看"用的，截图是"测"用的，两者并存
 */

'use strict';

const { spawn, execFileSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');
const { app } = require('electron');

const ROOT = path.resolve(__dirname, '..');

class ScrcpyManager {
  /**
   * @param {(eventName: string, payload: any) => void} emit IPC 事件转发器
   */
  constructor(emit) {
    this.emit = emit;
    this._mirrorProc = null;
    this._recordProc = null;
    this.mirroring = false;
    this.recording = false;
    this.recordPath = '';
    this.deviceSerial = '';
    this.deviceModel = '';
  }

  // ── 路径计算 ──────────────────────────────────────────────

  /**
   * 后端根目录（与 PythonManager.backendRoot 一致）。
   * 开发模式：项目根；打包模式：resources/backend。
   */
  get backendRoot() {
    return app.isPackaged
      ? path.join(process.resourcesPath, 'backend')
      : ROOT;
  }

  /** scrcpy 二进制路径（项目内置优先；macOS/Linux 回退 PATH——brew install scrcpy） */
  get scrcpyBin() {
    const exe = process.platform === 'win32' ? 'scrcpy.exe' : 'scrcpy';
    const p = app.isPackaged
      ? path.join(process.resourcesPath, 'scrcpy', exe)
      : path.join(ROOT, 'scrcpy', exe);
    if (fs.existsSync(p)) return p;
    // PATH 回退（brew 安装的 scrcpy 在 /opt/homebrew/bin 等位置）
    if (process.platform !== 'win32') {
      try {
        const found = execFileSync('which', ['scrcpy'], { timeout: 3000 }).toString().trim();
        if (found) return found;
      } catch { /* PATH 里没有 scrcpy */ }
    }
    return null;
  }

  /** scrcpy-server 路径（推到设备端的 Java 服务端；brew 版无独立 server 文件） */
  get scrcpyServer() {
    const p = app.isPackaged
      ? path.join(process.resourcesPath, 'scrcpy', 'scrcpy-server')
      : path.join(ROOT, 'scrcpy', 'scrcpy-server');
    return fs.existsSync(p) ? p : null;
  }

  /** adb 路径（项目内置优先；macOS/Linux 回退 PATH——brew android-platform-tools） */
  get adbPath() {
    const exe = process.platform === 'win32' ? 'adb.exe' : 'adb';
    const p = path.join(this.backendRoot, 'adb', exe);
    if (fs.existsSync(p)) return p;
    if (process.platform !== 'win32') {
      try {
        const found = execFileSync('which', ['adb'], { timeout: 3000 }).toString().trim();
        if (found) return found;
      } catch { /* PATH 里没有 adb */ }
    }
    return null;
  }

  /** scrcpy 是否可用（二进制存在） */
  isAvailable() {
    return !!this.scrcpyBin;
  }

  getStatus() {
    return {
      available: this.isAvailable(),
      mirroring: this.mirroring,
      recording: this.recording,
      recordPath: this.recordPath,
      deviceSerial: this.deviceSerial,
      deviceModel: this.deviceModel,
    };
  }

  // ── 内部方法 ──────────────────────────────────────────────

  /**
   * scrcpy 子进程共用环境变量。
   * 关键：ADB 和 SCRCPY_SERVER_PATH 指向项目内置的二进制，
   * 确保 scrcpy 与后端复用同一份 adb-server（不另起 daemon，不冲突）。
   */
  _spawnEnv() {
    const env = { ...process.env };
    if (this.adbPath) env.ADB = this.adbPath;
    if (this.scrcpyServer) env.SCRCPY_SERVER_PATH = this.scrcpyServer;
    return env;
  }

  /**
   * spawn scrcpy 子进程并接管生命周期。
   * @param {string[]} args
   * @param {'mirror'|'record'} kind
   * @returns {import('child_process').ChildProcess}
   */
  _spawnScrcpy(args, kind) {
    const proc = spawn(this.scrcpyBin, args, {
      env: this._spawnEnv(),
      stdio: ['ignore', 'pipe', 'pipe'],
      // 录屏是后台进程（无窗口），隐藏控制台；镜像需要弹窗不隐藏
      windowsHide: kind === 'record',
    });

    let stderrTail = '';
    proc.stderr?.on('data', (chunk) => {
      stderrTail = (stderrTail + chunk.toString('utf8')).slice(-2000);
    });

    let exited = false;
    const finalize = ({ error } = {}) => {
      if (exited) return;
      exited = true;
      if (proc._killTimer) { clearTimeout(proc._killTimer); proc._killTimer = null; }

      if (kind === 'mirror') {
        this._mirrorProc = null;
        this.mirroring = false;
      } else {
        this._recordProc = null;
        this.recording = false;
        this.recordPath = '';
      }
      this.emit('scrcpy:status', { ...this.getStatus(), error });
    };

    proc.on('error', (err) => {
      console.error(`[scrcpy:${kind}] spawn error:`, err);
      finalize({ error: 'scrcpy 启动失败: ' + err.message });
    });

    proc.on('exit', (code, signal) => {
      const isClean = code === 0 || code === null ||
        ['SIGINT', 'SIGTERM', 'SIGKILL'].includes(signal);
      const errTip = isClean ? undefined
        : stderrTail.trim().split('\n').slice(-2).join(' ') || `scrcpy 退出 code=${code}`;
      finalize({ error: errTip });
    });

    return proc;
  }

  /**
   * 停止子进程。
   * Mac/Linux：SIGTERM 优雅退出（scrcpy 写完录屏文件尾 moov）+ 3s SIGKILL 兜底。
   * Windows：镜像走 proc.kill()（硬杀，无需保存）；录屏也走 proc.kill()
   *   （MKV 格式对截断鲁棒，不像 MP4 需要写 moov 原子）。
   */
  _sigStop(proc) {
    if (!proc) return;
    try {
      if (process.platform !== 'win32') {
        proc.kill('SIGTERM');
        if (!proc._killTimer) {
          proc._killTimer = setTimeout(() => {
            try { proc.kill('SIGKILL'); } catch {}
          }, 3000);
          proc._killTimer.unref?.();
        }
      } else {
        proc.kill();
      }
    } catch {}
  }

  // ── 公开 API ──────────────────────────────────────────────

  /**
   * 启动镜像（独立置顶窗口）。
   * @param {string} serial 设备 serial
   * @param {string} model 设备型号（窗口标题用）
   * @returns {{ ok: boolean, error?: string }}
   */
  startMirror(serial, model) {
    if (!this.isAvailable()) {
      return { ok: false, error: '未检测到内置 scrcpy（scrcpy/scrcpy.exe 不存在）' };
    }
    if (this.mirroring) {
      return { ok: false, error: '镜像已在运行' };
    }

    this.deviceSerial = serial;
    this.deviceModel = model || serial;
    const title = `App 冷启测速 - ${this.deviceModel}`;
    // --always-on-top：SDL 内部实现置顶，无需系统权限，Mac/Win 通用
    // -s：指定设备；--window-title：窗口标题
    const args = ['-s', serial, '--always-on-top', '--window-title', title];
    this._mirrorProc = this._spawnScrcpy(args, 'mirror');
    this.mirroring = true;
    this.emit('scrcpy:status', this.getStatus());
    console.log(`[scrcpy] 镜像已启动: ${serial} (${this.deviceModel})`);
    return { ok: true };
  }

  /**
   * 停止镜像。
   */
  stopMirror() {
    if (!this.mirroring) return { ok: false, error: '镜像未运行' };
    this._sigStop(this._mirrorProc);
    return { ok: true };
  }

  /**
   * 开始录屏（需镜像运行中）。
   * 录到临时目录，停止后由调用方（前端）选择保存位置。
   * Windows 用 .mkv（对截断鲁棒），Mac/Linux 用 .mp4。
   *
   * @param {string} serial
   * @returns {{ ok: boolean, recordPath?: string, error?: string }}
   */
  startRecord(serial) {
    if (!this.isAvailable()) {
      return { ok: false, error: '未检测到内置 scrcpy' };
    }
    if (this.recording) {
      return { ok: false, error: '录屏已在进行中' };
    }
    if (!this.mirroring) {
      return { ok: false, error: '录屏需先开启镜像' };
    }

    const isWin = process.platform === 'win32';
    const ext = isWin ? 'mkv' : 'mp4';
    const tempDir = os.tmpdir();
    const recordPath = path.join(tempDir, `_cst_record_${Date.now()}.${ext}`);

    // --no-window --no-playback：后台无窗口录制
    // --record：输出文件路径
    // --max-size 1280 --max-fps 30：720p 30fps，兼顾清晰度与体积
    const args = [
      '-s', serial,
      '--no-window', '--no-playback',
      '--record', recordPath,
      '--max-size', '1280',
      '--max-fps', '30',
    ];
    this._recordProc = this._spawnScrcpy(args, 'record');
    this.recording = true;
    this.recordPath = recordPath;
    this.emit('scrcpy:status', this.getStatus());
    console.log(`[scrcpy] 录屏已开始: ${recordPath}`);
    return { ok: true, recordPath };
  }

  /**
   * 停止录屏，返回录制的文件路径。
   * @returns {{ ok: boolean, recordPath?: string, error?: string }}
   */
  stopRecord() {
    if (!this.recording) return { ok: false, error: '录屏未进行' };
    const path = this.recordPath;
    this._sigStop(this._recordProc);
    console.log(`[scrcpy] 录屏已停止: ${path}`);
    return { ok: true, recordPath: path };
  }

  /**
   * 应用退出前清理：杀掉镜像和录屏子进程。
   */
  dispose() {
    if (this._recordProc) { this._sigStop(this._recordProc); }
    if (this._mirrorProc) { this._sigStop(this._mirrorProc); }
  }
}

module.exports = { ScrcpyManager };
