// 同時実行: node conc_bench.mjs <prod|local>
// 別々の地点から同時に N 本投げ、1 本ごとの応答時間を見る。本番は Rate Limiting（20 回/10 秒/IP）にかからないよう回の間を 11 秒あける
const target = process.argv[2] ?? 'prod';
const BASE = target === 'prod' ? 'https://ksj-route-search.shi-works-worker.workers.dev/api' : 'http://localhost:8000';
const sleep = ms => new Promise(r => setTimeout(r, ms));
// 関東の 8 地点（結果が被らないよう少しずつずらす）
const PTS = [[35.6812, 139.7671], [35.4658, 139.6223], [35.8617, 139.6455], [35.6074, 140.1065],
  [35.6580, 139.7016], [35.7295, 139.7109], [35.5308, 139.6970], [35.7910, 139.8016]];
const one = async (url) => { const t = performance.now(); const r = await fetch(url); const b = await r.arrayBuffer(); return { ms: performance.now() - t, status: r.status, n: b.byteLength }; };
for (const [kind, mk] of [
  ['到達圏 120 分', ([a, b]) => `${BASE}/reachability?lat=${a}&lon=${b}&limit_min=120&road_class=auto&use_expressway=true&format=bin`],
  ['到達圏 480 分', ([a, b]) => `${BASE}/reachability?lat=${a}&lon=${b}&limit_min=480&road_class=auto&use_expressway=true&format=bin`],
  ['経路 →大阪', ([a, b]) => `${BASE}/route?from_lat=${a}&from_lon=${b}&to_lat=34.7025&to_lon=135.4959&use_expressway=true`],
]) {
  for (const n of [1, 2, 4, 8]) {
    const t = performance.now();
    const rs = await Promise.all(PTS.slice(0, n).map(p => one(mk(p))));
    const wall = performance.now() - t;
    const ms = rs.map(r => r.ms).sort((x, y) => x - y);
    const bad = rs.filter(r => r.status !== 200).map(r => r.status);
    console.log(`${kind} 同時 ${n}: 最速 ${ms[0].toFixed(0)} / 中央 ${ms[ms.length >> 1].toFixed(0)} / 最遅 ${ms.at(-1).toFixed(0)} ms（全部終わるまで ${wall.toFixed(0)} ms）${bad.length ? ' エラー ' + bad.join(',') : ''}`);
    await sleep(target === 'prod' ? 11000 : 500);
  }
}
