// 统计纯函数（UI 与导出共用）——集中一处防止复制导致口径漂移。
// 历史教训：毫秒当秒导出（上万秒）、多设备混算、并列极值剔错都是复制副本
// 口径漂移出来的。本文件同时被 Node 测试直接加载（tests/stats-core.test.mjs），
// 改动必须跑 node --test。
(function (g) {
  'use strict';

  // 截尾均值：n>=3 剔 1 个 max + 1 个 min（并列只剔第一个匹配，与 AGENTS §2.3 一致），
  // 不足 3 条全量平均。vals 不会被修改。
  // 返回 { mean, max, min, maxIdx, minIdx, raw }；max/min/maxIdx/minIdx 在 n<3 时
  // 为 null/-1（未剔除）；空数组返回 null。
  function trimValues(vals) {
    if (!vals || !vals.length) return null;
    const raw = vals.slice();
    if (vals.length >= 3) {
      const max = Math.max(...vals), min = Math.min(...vals);
      const maxIdx = vals.indexOf(max), minIdx = vals.indexOf(min);
      const kept = vals.filter((_, i) => i !== maxIdx && i !== minIdx);
      return { mean: kept.reduce((a, b) => a + b, 0) / kept.length, max, min, maxIdx, minIdx, raw };
    }
    const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
    return { mean, max: null, min: null, maxIdx: -1, minIdx: -1, raw };
  }

  // 毫秒 → 秒（保留 3 位小数）。界面显示与导出统一用这个，别再手写 ms/1000。
  function sec(ms) { return (ms / 1000).toFixed(3); }

  g.StatsCore = { trimValues, sec };
})(typeof window !== 'undefined' ? window : globalThis);
