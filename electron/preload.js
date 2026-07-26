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
   * @param {Object} options - { title, defaultPath, filters }
   * @returns {Promise<{canceled: boolean, filePath?: string}>}
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
    ipcRenderer.on('menu-command', (event, cmd) => callback(cmd));
  },
});
