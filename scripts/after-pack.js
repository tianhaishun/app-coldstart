/**
 * electron-builder afterPack 钩子。
 *
 * 在 app 目录组装完成、NSIS 打包之前执行：
 *   1. 用内置 Python 将 server.py 编译为 server.pyc（源码保护）
 *   2. 删除 server.py，只保留字节码
 *
 * 这样最终安装包里不含明文 Python 源码。
 */

'use strict';

const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

exports.default = async function (context) {
  const backendDir = path.join(context.appOutDir, 'resources', 'backend');
  const serverPy = path.join(backendDir, 'server.py');

  if (!fs.existsSync(serverPy)) {
    console.log('  [after-pack] server.py 不存在，跳过编译');
    return;
  }

  // 优先用内置 Python 编译（保证字节码 magic number 匹配）
  const embedPython = path.join(context.appOutDir, 'resources', 'python-embed', 'python.exe');
  const venvPython = path.join(__dirname, '..', '.venv', 'Scripts', 'python.exe');
  const pythonExe = fs.existsSync(embedPython) ? embedPython : venvPython;

  console.log('  [after-pack] 编译 server.py → server.pyc ...');

  try {
    execFileSync(pythonExe, ['-m', 'compileall', '-b', serverPy], {
      cwd: backendDir,
      stdio: 'pipe',
      timeout: 30_000,
    });
  } catch (e) {
    console.log('  [after-pack] 编译失败，保留 server.py');
    return;
  }

  const serverPyc = path.join(backendDir, 'server.pyc');
  if (fs.existsSync(serverPyc)) {
    fs.unlinkSync(serverPy);
    // 清理 __pycache__（compileall 可能也生成了）
    const pycache = path.join(backendDir, '__pycache__');
    if (fs.existsSync(pycache)) {
      fs.rmSync(pycache, { recursive: true });
    }
    const sizeKB = (fs.statSync(serverPyc).size / 1024).toFixed(0);
    console.log(`  [after-pack] ✓ server.pyc (${sizeKB} KB)，已删除 server.py`);
  } else {
    console.log('  [after-pack] server.pyc 未生成，保留 server.py');
  }
};
