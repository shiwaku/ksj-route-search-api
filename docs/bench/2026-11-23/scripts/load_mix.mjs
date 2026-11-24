// 負荷試験: node load_mix.mjs <base> <N,...> [scenario] [gap_ms]
//   base     例: http://127.0.0.1:8002（手元）/ https://…workers.dev/api（本番。Access があるので、実際はログイン済みのブラウザのコンソールで同じ処理を base='/api' として流した）
//   scenario mix（既定）= 来場者を想定した混在 / car = 車の到達圏 120 分だけ / walk = 徒歩の到達圏 30 分だけ
// N 人が同時に 1 回ずつタップした状況を作る。リクエストごとに別の X-Client-Id を付ける（端末ごとの回数制限を人数分に分ける）
const base = process.argv[2] ?? 'http://127.0.0.1:8002';
const Ns = (process.argv[3] ?? '30,60,100').split(',').map(Number);
const scenario = process.argv[4] ?? 'mix';
const gap = Number(process.argv[5] ?? 15000);

const rnd = (c, d) => c + (Math.random() * 2 - 1) * d;
// 札幌圏（既定の表示）と会場まわり。徒歩は市街地（札幌・北広島・旭川・函館）
const CAR_CENTERS = [[43.0687, 141.3508], [42.978, 141.563], [43.06, 141.45]];
const WALK_CENTERS = [[43.0687, 141.3508], [42.978, 141.563], [43.7706, 142.365], [41.7737, 140.7264]];
const pick = a => a[Math.floor(Math.random() * a.length)];

const KINDS = {
  car_reach120: () => { const [a, b] = pick(CAR_CENTERS); return `/reachability?lat=${rnd(a, .08)}&lon=${rnd(b, .1)}&limit_min=120&road_class=auto&use_expressway=true&format=bin`; },
  car_route: () => { const [a, b] = pick(CAR_CENTERS); return `/route?from_lat=${rnd(a, .05)}&from_lon=${rnd(b, .05)}&to_lat=${rnd(42.79, .02)}&to_lon=${rnd(141.68, .02)}&use_expressway=true`; },
  walk_reach30: () => { const [a, b] = pick(WALK_CENTERS); return `/reachability?lat=${rnd(a, .01)}&lon=${rnd(b, .01)}&limit_min=30&mode=walk&walk_kmh=4&format=bin`; },
  walk_reach120: () => { const [a, b] = pick(WALK_CENTERS); return `/reachability?lat=${rnd(a, .01)}&lon=${rnd(b, .01)}&limit_min=120&mode=walk&walk_kmh=4&format=bin`; },
  walk_route: () => { const [a, b] = pick(WALK_CENTERS); return `/route?from_lat=${rnd(a, .01)}&from_lon=${rnd(b, .01)}&to_lat=${rnd(a, .015)}&to_lon=${rnd(b, .015)}&mode=walk&walk_kmh=4`; },
};
// 来場者の想定: 車の到達圏が半分、残りを徒歩と経路で分ける
const MIX = [['car_reach120', 40], ['walk_reach30', 25], ['walk_reach120', 5], ['walk_route', 15], ['car_route', 15]];
const choose = () => {
  if (scenario === 'car') return 'car_reach120';
  if (scenario === 'walk') return 'walk_reach30';
  let r = Math.random() * 100;
  for (const [k, w] of MIX) { if ((r -= w) < 0) return k; }
  return MIX[0][0];
};

const one = async (kind) => {
  const t = performance.now();
  try {
    const r = await fetch(base + KINDS[kind](), { headers: { 'X-Client-Id': crypto.randomUUID() } });
    await r.arrayBuffer();
    return { kind, ms: performance.now() - t, status: r.status, inst: r.headers.get('X-Container-Instance') };
  } catch (e) {
    return { kind, ms: performance.now() - t, status: 'ERR' };
  }
};
const q = (a, p) => a[Math.min(a.length - 1, Math.floor(a.length * p))];
const fmt = a => { const s = a.map(x => x.ms).sort((x, y) => x - y); return s.length ? `n=${s.length} 中央 ${(q(s, .5) / 1000).toFixed(1)} / 90% ${(q(s, .9) / 1000).toFixed(1)} / 最遅 ${(s.at(-1) / 1000).toFixed(1)} 秒` : '-'; };

for (const n of Ns) {
  const t = performance.now();
  const rs = await Promise.all(Array.from({ length: n }, () => one(choose())));
  const wall = performance.now() - t;
  const bad = rs.filter(r => r.status !== 200);
  console.log(`\n=== ${scenario} 同時 ${n} 人（全部終わるまで ${(wall / 1000).toFixed(1)} 秒）エラー ${bad.length}${bad.length ? ' ' + [...new Set(bad.map(b => b.status))].join(',') : ''}`);
  console.log(`  全体          ${fmt(rs)}`);
  for (const k of Object.keys(KINDS)) { const sub = rs.filter(r => r.kind === k); if (sub.length) console.log(`  ${k.padEnd(13)} ${fmt(sub)}`); }
  // 本番で複数台のとき、Worker が返す X-Container-Instance で台ごとに分ける（手元の API には無い）
  for (const i of [...new Set(rs.map(r => r.inst).filter(x => x != null))].sort()) console.log(`  台${i.padEnd(11)} ${fmt(rs.filter(r => r.inst === i))}`);
  if (n !== Ns.at(-1)) await new Promise(r => setTimeout(r, gap));
}
