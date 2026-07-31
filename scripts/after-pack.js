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

  // 源码保护策略：
  //   - Windows（内嵌 Python 3.11.9 固定）：构建时编译 server.py → server.pyc，
  //     运行时是同一个 Python，magic number 必然匹配。✅
  //   - Mac（用户自己的 Python，版本不可控）：构建时编译的 .pyc 可能和用户
  //     的 Python 版本不匹配（bad magic number → App 打不开！）。
  //     因此 Mac 包保留 server.py 源码，由 python-manager.js 在首次启动时
  //     用实际运行的 Python 做「运行时编译」（同样产 .pyc + 删源码）。
  const embedPython = path.join(context.appOutDir, 'resources', 'python-embed', 'python.exe');

  if (!fs.existsSync(embedPython)) {
    console.log('  [after-pack] 非内嵌 Python 平台（Mac），保留 server.py，由运行时编译保护');
    return;
  }

  console.log('  [after-pack] 编译 server.py → server.pyc ...');

  try {
    execFileSync(embedPython, ['-m', 'compileall', '-b', serverPy], {
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
