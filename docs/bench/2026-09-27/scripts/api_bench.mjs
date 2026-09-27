// API 単体の計測: node api_bench.mjs <prod|local> [reps]
// 到達圏（地点 × 打ち切り × 高速あり/なし）と経路（区間 × 高速あり/なし）。各条件 reps 回の中央値
// 本番は Rate Limiting（20 回/10 秒/IP）にかからないよう、リクエストの間を GAP ms あける
import fs from 'node:fs';
const target = process.argv[2] ?? 'prod';
const REPS = Number(process.argv[3] ?? 3);
const BASE = target === 'prod' ? 'https://ksj-route-search.shi-works-worker.workers.dev/api' : 'http://localhost:8000';
const GAP = target === 'prod' ? 700 : 50;
const sleep = ms => new Promise(r => setTimeout(r, ms));

const ORIGINS = {
  東京駅: [35.6812, 139.7671], 大阪駅: [34.7025, 135.4959], 札幌駅: [43.0687, 141.3508], 松本駅: [36.2309, 137.9644],
};
const LIMITS = [15, 60, 120, 480, 1920];
const PAIRS = {
  '東京→横浜': [ORIGINS.東京駅, [35.4658, 139.6223]],
  '東京→名古屋': [ORIGINS.東京駅, [35.1709, 136.8815]],
  '東京→大阪': [ORIGINS.東京駅, ORIGINS.大阪駅],
  '東京→福岡': [ORIGINS.東京駅, [33.5897, 130.4207]],
  '札幌→旭川': [ORIGINS.札幌駅, [43.7631, 142.3582]],
};

async function hit(url, bin) {
  const t0 = performance.now();
  const res = await fetch(url);
  const tHead = performance.now() - t0;
  const buf = await res.arrayBuffer();
  const total = performance.now() - t0;
  if (res.status !== 200) return { status: res.status, total, tHead };
  let body;
  if (bin && !res.headers.get('content-type')?.includes('json')) {
    const h = new DataView(buf).getUint32(0, true);
    body = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, h)));
  } else body = JSON.parse(new TextDecoder().decode(buf));
  return { status: 200, total, tHead, server: body.elapsed_ms, wire: Number(res.headers.get('content-length') ?? 0) || null, raw: buf.byteLength, body };
}
const med = a => { const s = [...a].sort((x, y) => x - y); return s[s.length >> 1]; };

async function run(label, url, bin, extra) {
  const rs = [];
  for (let i = 0; i < REPS; i++) { const r = await hit(url, bin); rs.push(r); await sleep(GAP); if (r.status !== 200) break; }
  const ok = rs.filter(r => r.status === 200);
  if (!ok.length) return { label, status: rs[0].status };
  const row = {
    label, total: med(ok.map(r => r.total)), first: ok[0].total, server: med(ok.map(r => r.server)),
    head: med(ok.map(r => r.tHead)), raw: ok[0].raw, ...extra(ok[0].body),
  };
  row.net = row.total - row.server;   // サーバー内の計算以外（転送・シリアライズ・Worker 経由のぶん）
  return row;
}

const out = { target, reps: REPS, at: new Date().toISOString(), health: await (await fetch(BASE + '/health')).json(), reach: [], route: [] };
console.log(`# ${target} ${BASE} reps=${REPS}  location=${out.health.location ?? '-'}`);
for (const [name, [lat, lon]] of Object.entries(ORIGINS)) for (const lim of LIMITS) for (const ex of [true, false]) {
  const url = `${BASE}/reachability?lat=${lat}&lon=${lon}&limit_min=${lim}&road_class=auto&use_expressway=${ex}&format=bin`;
  const r = await run(`${name} ${lim}分 高速${ex ? 'あり' : 'なし'}`, url, true, b => ({ count: b.count, tier: b.tier, cls: b.road_class_applied }));
  Object.assign(r, { origin: name, limit: lim, ex });
  out.reach.push(r);
  console.log(r.status ? `${r.label}: HTTP ${r.status}` :
    `${r.label.padEnd(22)} 合計 ${r.total.toFixed(0).padStart(5)} ms（初回 ${r.first.toFixed(0)}）/ サーバー ${r.server.toFixed(0).padStart(5)} / 差 ${r.net.toFixed(0).padStart(5)} | ${r.count.toLocaleString().padStart(9)} 本 ${r.cls} ${(r.raw / 1e6).toFixed(2)} MB`);
}
for (const [name, [[a, b], [c, d]]] of Object.entries(PAIRS)) for (const ex of [true, false]) {
  const url = `${BASE}/route?from_lat=${a}&from_lon=${b}&to_lat=${c}&to_lon=${d}&use_expressway=${ex}`;
  const r = await run(`${name} 高速${ex ? 'あり' : 'なし'}`, url, false, b => ({ km: b.summary?.dist_m / 1000, min: b.summary?.time_min, links: b.summary?.link_count }));
  Object.assign(r, { pair: name, ex });
  out.route.push(r);
  console.log(r.status ? `${r.label}: HTTP ${r.status}` :
    `${r.label.padEnd(22)} 合計 ${r.total.toFixed(0).padStart(5)} ms（初回 ${r.first.toFixed(0)}）/ サーバー ${r.server.toFixed(0).padStart(5)} / 差 ${r.net.toFixed(0).padStart(5)} | ${r.km?.toFixed(0)} km ${r.min?.toFixed(0)} 分 ${(r.raw / 1e3).toFixed(0)} KB`);
}
fs.writeFileSync(`api_${target}.json`, JSON.stringify(out, (k, v) => typeof v === 'number' ? Math.round(v * 10) / 10 : v, 1));
