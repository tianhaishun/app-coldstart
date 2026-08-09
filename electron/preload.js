/**
 * Preload 脚本 — 渲染进程与主进程之间的安全 IPC 桥。
 *
 * 安全原则：
 *   - contextIsolation: true  → 渲染进程的 JS 上下文与 Node.js 完全隔离
 *   - nodeIntegration: false  → 渲染进程无法直接 require Node.js 模块
 *   - 只通过 contextBridge 暴露最小化的、显式声明的 API
 *
 * 暴露的 API：
 *   window.electronAPI.platform        — 当前操作系统
 *   window.electronAPI.isElectron      — 恒为 true（前端检测用）
 *   window.electronAPI.versions        — 版本号信息
 *   window.electronAPI.openFileDialog  — 原生文件选择对话框
 *   window.electronAPI.showAlert       — 原生信息提示框（替代 alert）
 *   window.electronAPI.showConfirm     — 原生确认对话框（替代 confirm）
 *   window.electronAPI.onMenuCommand   — 注册菜单命令回调
 *   window.electronAPI.onBackendStatus — 监听后端状态变化（online/offline）
 */

'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  platform: process.platform,
  isElectron: true,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  },

  /**
   * 原生文件选择对话框。
   * 主进程取得路径后会直接读文件内容返回 base64（避免渲染层 fetch(file://) 不稳定）。
   * @param {Object} options - { title, defaultPath, filters }
   * @returns {Promise<{canceled: boolean, filePath?: string, name?: string,
   *   size?: number, dataUrl?: string, readError?: string}>}
   *   - dataUrl：文件 base64（成功读文件时）
   *   - readError：主进程读文件失败原因（无 dataUrl 时）
   */
  openFileDialog: (options) => ipcRenderer.invoke('dialog:openFile', options),

  /**
   * 原生消息提示框（替代 alert）。
   * @param {string} message - 标题消息
   * @param {string} [detail] - 详细内容（可选，显示在标题下方灰色字）
   * @param {string} [type] - 'info' | 'warning' | 'error'
   */
  showAlert: (message, detail, type) =>
    ipcRenderer.invoke('dialog:showMessage', {
      message: String(message || ''),
      detail: detail ? String(detail) : '',
      type: type || 'info',
      buttons: ['确定'],
    }),

  /**
   * 原生确认对话框（替代 confirm）。
   * @param {string} message - 标题消息
   * @param {string} [detail] - 详细内容
   * @param {string} [type] - 'question' | 'warning'
   * @returns {Promise<boolean>} 用户点击"确定"返回 true，"取消"返回 false
   */
  showConfirm: (message, detail, type) =>
    ipcRenderer.invoke('dialog:showMessage', {
      message: String(message || ''),
      detail: detail ? String(detail) : '',
      type: type || 'question',
      buttons: ['确定', '取消'],
      defaultId: 0,
      cancelId: 1,
    }).then(result => result.response === 0),

  /**
   * 注册菜单命令回调。
   * 菜单项点击时，主进程通过 IPC 发送命令字符串，前端据此触发对应操作。
   * @param {Function} callback - 接收命令字符串的回调
   */
  onMenuCommand: (callback) => {
    // 去重：多次调用只保留最后一个监听器（避免热重载/重复注册导致回调多次触发）
    ipcRenderer.removeAllListeners('menu-command');
    ipcRenderer.on('menu-command', (event, cmd) => callback(cmd));
  },

  /**
   * 监听后端状态变化（online/offline）。
   * 主进程在后端意外退出时 send('backend-status','offline')，前端展示 overlay。
   * @param {Function} callback - 接收 'online' | 'offline'
   */
  onBackendStatus: (callback) => {
    ipcRenderer.removeAllListeners('backend-status');
    ipcRenderer.on('backend-status', (event, status) => callback(status));
  },

  // ── scrcpy 镜像 / 录屏 ──
  startMirror: (serial, model) => ipcRenderer.invoke('scrcpy:mirror:start', { serial, model }),
  stopMirror: () => ipcRenderer.invoke('scrcpy:mirror:stop'),
  startRecord: (serial) => ipcRenderer.invoke('scrcpy:record:start', { serial }),
  stopRecord: () => ipcRenderer.invoke('scrcpy:record:stop'),
  getScrcpyStatus: () => ipcRenderer.invoke('scrcpy:getStatus'),
  onScrcpyStatus: (callback) => {
    ipcRenderer.removeAllListeners('scrcpy:status');
    ipcRenderer.on('scrcpy:status', (event, status) => callback(status));
  },

  // 设备热插拔即时通知（adb track-devices 检测到变化时触发）
  onDevicesChanged: (callback) => {
    ipcRenderer.removeAllListeners('devices:changed');
    ipcRenderer.on('devices:changed', () => callback());
  },
});
