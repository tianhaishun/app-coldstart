// stats-core.js 纯函数测试（node --test，零依赖）。
// 统计口径的回归防线：历史 bug（毫秒当秒导出上万秒、并列极值剔错、
// 多设备混算）都在这些共享函数附近。改 static/stats-core.js 必须跑本测试。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const require = createRequire(import.meta.url);
const root = path.dirname(fileURLToPath(import.meta.url));
require(path.join(root, '..', 'static', 'stats-core.js'));
const { trimValues, sec } = globalThis.StatsCore;

test('trimValues: n>=3 剔第一个 max 和第一个 min', () => {
  const t = trimValues([100, 200, 300, 400, 500]);
  assert.equal(t.mean, 300);            // (200+300+400)/3
  assert.equal(t.max, 500);
  assert.equal(t.min, 100);
  assert.equal(t.maxIdx, 4);
  assert.equal(t.minIdx, 0);
  assert.deepEqual(t.raw, [100, 200, 300, 400, 500]);
});

test('trimValues: 并列极值只剔第一个匹配（与 AGENTS §2.3 一致）', () => {
  // 两个 500 并列 max：只剔 idx0，idx3 的 500 保留
  const t = trimValues([500, 100, 200, 500, 300]);
  assert.equal(t.maxIdx, 0);
  assert.equal(t.minIdx, 1);
  assert.equal(t.mean, (200 + 500 + 300) / 3);
});

test('trimValues: n<3 全量平均不剔，极值标记为 null/-1', () => {
  const t = trimValues([100, 200]);
  assert.equal(t.mean, 150);
  assert.equal(t.max, null);
  assert.equal(t.min, null);
  assert.equal(t.maxIdx, -1);
  assert.equal(t.minIdx, -1);
});

test('trimValues: 单元素与空数组', () => {
  assert.equal(trimValues([42]).mean, 42);
  assert.equal(trimValues([]), null);
  assert.equal(trimValues(null), null);
});

test('trimValues: 不修改入参数组', () => {
  const vals = [1, 2, 3, 4, 5];
  trimValues(vals);
  assert.deepEqual(vals, [1, 2, 3, 4, 5]);
});

test('sec: 毫秒转秒保留 3 位（历史 bug：毫秒当秒导出显示上万秒）', () => {
  assert.equal(sec(12345), '12.345');
  assert.equal(sec(1000), '1.000');
  assert.equal(sec(0), '0.000');
  assert.equal(sec(1234.5678), '1.235');  // toFixed 四舍五入
});

test('trimValues: 真实样本量级（毫秒值）截尾均值正确', () => {
  // 5 个样本：2.0s / 1.5s / 1.7s / 3.5s / 1.6s（ms）
  const t = trimValues([2000, 1500, 1700, 3500, 1600]);
  assert.equal(t.mean, (2000 + 1700 + 1600) / 3);  // 剔 1500 与 3500
});
