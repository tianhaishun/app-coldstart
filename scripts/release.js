/**
 * 一键发布脚本（完整流程）。
 *
 * 流程：
 *   1. 构建嵌入式 Python 运行时（首次或 --force-embed 时）
 *   2. electron-builder --win（含 after-pack 钩子：server.py → server.pyc）
 *   3. 生成 Word 发布说明
 *   4. 打包发布 ZIP（安装包 + 安装说明 + Word 文档）
 *
 * 用法：
 *   npm run release                  # 完整流程
 *   npm run release -- --skip-embed  # 跳过嵌入式 Python 构建（已构建过）
 *   npm run release -- --skip-build  # 跳过构建（已有 setup.exe）
 *
 * 产出：
 *   publish/
 *   ├── v2.0.0/
 *   │   ├── AppColdStart-2.0.0-setup.exe
 *   │   └── 发布说明-v2.0.0.docx
 *   └── AppColdStart-v2.0.0.zip       ← 最终分发文件
 */

'use strict';

const { execSync } = require('child_process');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '..');
const PYTHON = path.join(ROOT, '.venv', 'Scripts', 'python.exe');
const EMBED_DIR = path.join(ROOT, 'python-embed');

const args = process.argv.slice(2);
const skipEmbed = args.includes('--skip-embed');
const skipBuild = args.includes('--skip-build');
const forceEmbed = args.includes('--force-embed');

function run(cmd, label, cwd = ROOT) {
  console.log(`\n▶ ${label}`);
  console.log(`  $ ${cmd}\n`);
  execSync(cmd, { stdio: 'inherit', cwd });
}

function logBox(lines) {
  const width = Math.max(...lines.map(l => l.length)) + 4;
  const top = '╔' + '═'.repeat(width - 2) + '╗';
  const bot = '╚' + '═'.repeat(width - 2) + '╝';
  console.log('\n' + top);
  for (const line of lines) {
    const pad = ' '.repeat(width - 2 - line.length);
    console.log('║ ' + line + pad + ' ║');
  }
  console.log(bot + '\n');
}

function main() {
  const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf-8'));
  const version = pkg.version;

  logBox([
    `  App 冷启测速 v${version} 发布`,
    ``,
    `  skip-embed: ${skipEmbed}  skip-build: ${skipBuild}`,
  ]);

  // ══════════════════════════════════════════════════════════════════════════
  // 1. 构建嵌入式 Python（除非跳过）
  // ══════════════════════════════════════════════════════════════════════════
  if (!skipEmbed) {
    const embedPython = path.join(EMBED_DIR, 'python.exe');
    if (fs.existsSync(embedPython) && !forceEmbed) {
      console.log('\n⏭  python-embed/ 已存在，跳过构建（--force-embed 强制重建）');
    } else {
      const pyExe = fs.existsSync(PYTHON) ? `"${PYTHON}"` : 'python';
      const forceFlag = forceEmbed ? '--force' : '';
      run(`${pyExe} scripts/build-python-embed.py ${forceFlag}`, '构建嵌入式 Python 运行时');
    }
  }

  // ══════════════════════════════════════════════════════════════════════════
  // 2. 构建安装包
  // ══════════════════════════════════════════════════════════════════════════
  if (!skipBuild) {
    run('npm run build:win', '构建 NSIS 安装包（含 after-pack 源码保护）');
  } else {
    console.log('\n⏭  跳过构建（--skip-build）');
  }

  // 验证安装包
  const releaseDir = path.join(ROOT, 'release');
  const setupFiles = fs.readdirSync(releaseDir).filter(f => f.endsWith('-setup.exe'));
  if (setupFiles.length === 0) {
    console.error('\n❌ 未找到安装包（release/*-setup.exe）');
    process.exit(1);
  }
  console.log(`\n✅ 安装包: ${setupFiles[0]}`);

  // ══════════════════════════════════════════════════════════════════════════
  // 3. 生成 Word 发布说明
  // ══════════════════════════════════════════════════════════════════════════
  const pyExe = fs.existsSync(PYTHON) ? `"${PYTHON}"` : 'python';
  run(`${pyExe} scripts/gen-release-doc.py`, '生成 Word 发布说明');

  // ══════════════════════════════════════════════════════════════════════════
  // 4. 打包 ZIP
  // ══════════════════════════════════════════════════════════════════════════
  run(`${pyExe} scripts/make-release-zip.py`, '打包发布 ZIP');

  // ══════════════════════════════════════════════════════════════════════════
  // 完成
  // ══════════════════════════════════════════════════════════════════════════
  const zipPath = path.join(ROOT, 'publish', `AppColdStart-v${version}.zip`);
  const zipSize = fs.existsSync(zipPath)
    ? `${(fs.statSync(zipPath).size / (1024 * 1024)).toFixed(0)} MB`
    : '?';

  logBox([
    '  ✅ 发布完成！',
    '',
    `  最终分发文件:`,
    `  publish/AppColdStart-v${version}.zip (${zipSize})`,
    '',
    '  内容:',
    `    • ${setupFiles[0]}（自包含安装包）`,
    `    • 安装说明.txt`,
    `    • 发布说明-v${version}.docx`,
    '',
    '  下一步:',
    '    1. 打开 ZIP 检查内容',
    '    2. 上传到 GitHub Release / 共享盘',
    '    3. Word 文档导入飞书/钉钉/语雀',
  ]);
}

main();
