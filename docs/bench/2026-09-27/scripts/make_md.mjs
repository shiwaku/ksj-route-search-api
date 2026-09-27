// 計測結果の JSON から docs/bench/2026-09-27/README.md の表を作る（本文は手で書いた head/tail を挟む）
import fs from 'node:fs';
const OUT = 'C:/Users/yshiw/Documents/GIS/ksj-route-search-api/docs/bench/2026-09-27';
const P = JSON.parse(fs.readFileSync('api_prod.json')), L = JSON.parse(fs.readFileSync('api_local.json'));
const C = JSON.parse(fs.readFileSync('conc.json'));
const NAMES = ['GPU', 'GPUなし', 'CPU4倍遅い', '回線遅い', 'スマホ相当'];
const U = NAMES.flatMap(n => JSON.parse(fs.readFileSync(`ui_${n}.json`)));
const r = Math.round, s1 = ms => (ms / 1000).toFixed(1) + ' 秒';
const o = [];
const t = (h, rows) => { o.push('| ' + h.join(' | ') + ' |', '|' + h.map(() => '---').join('|') + '|', ...rows.map(x => '| ' + x.join(' | ') + ' |'), ''); };

o.push('<!-- TABLES:API -->');
o.push('### 1-1. 到達圏', '', '合計 = 受け取り終わるまで（ms）。サーバー = レスポンスの `elapsed_ms`（サーバー内の計算）。本数のかっこは広域で間引いた道路種別（`road_class_applied`）', '');
t(['起点', '打ち切り', '高速', '本数', '本番 合計', '本番 サーバー', 'ローカル 合計', 'ローカル サーバー', 'サイズ'],
  P.reach.map((p, i) => { const l = L.reach[i]; return [p.origin, `${p.limit} 分`, p.ex ? 'あり' : 'なし', `${p.count.toLocaleString()}（${p.cls}）`, r(p.total), r(p.server), r(l.total), r(l.server), `${(p.raw / 1e6).toFixed(2)} MB`]; }));
o.push('### 1-2. 経路', '');
t(['区間', '高速', '距離', '本番 合計', '本番 サーバー', 'ローカル 合計', 'ローカル サーバー'],
  P.route.map((p, i) => { const l = L.route[i]; return [p.pair, p.ex ? 'あり' : 'なし', `${r(p.km)} km`, r(p.total), r(p.server), r(l.total), r(l.server)]; }));
o.push('### 1-3. 同時アクセス', '', '関東の別々の地点から N 本を一斉に投げたときの 1 本ごとの応答時間（最速 / 中央 / 最遅、ms）。経路は各地点→大阪駅', '');
for (const tg of ['prod', 'local']) {
  o.push(`**${tg === 'prod' ? '本番' : 'ローカル（0.5 vCPU・4 GiB）'}**`, '');
  t(['種類', '同時 1', '同時 2', '同時 4', '同時 8'], Object.entries(C[tg]).map(([k, v]) => [k.replace('経路 東京→大阪', '経路 →大阪'), ...v.map(x => x.join(' / '))]));
}

o.push('<!-- TABLES:UI -->');
const find = (n, z, k, lim) => U.find(x => x.profile === n && x.z === z && x.kind === k && (lim === undefined || x.limit === lim));
o.push('### 2-1. クリックしてから色が付くまで', '', '東京駅をクリック。かっこは（取得 = リクエスト送信〜受信完了 / 描画 = 画面の「描画」表示）ms', '');
t(['条件', 'ズーム', '開いて操作できるまで', '30 分', '120 分', '480 分'],
  NAMES.flatMap(n => [8, 10, 13].map(z => [n, z, s1(find(n, z, 'load').tReady),
    ...[30, 120, 480].map(lim => { const x = find(n, z, 'reach', lim); return `${s1(x.click)}（${x.fetch} / ${x.paint}）`; })])));
o.push('### 2-2. 地図を動かしているときのフレーム間隔', '', '480 分を表示したまま、約 0.5 秒かけて画面の 4 割ほどドラッグ。60 fps なら 17 ms', '');
t(['条件', 'ズーム', '中央値', 'p90', '最大', '50 ms 超'],
  NAMES.flatMap(n => [8, 10, 13].map(z => { const x = find(n, z, 'pan'); return n === 'スマホ相当' ? [n, z, '（無効: 地図が動かなかった）', '', '', ''] : [n, z, x.med, x.p90, x.max, `${x.longFrames} 回`]; })));
o.push('### 2-3. 経路（2 点目のクリックから表示まで）', '');
t(['条件', '東京→横浜', '東京→大阪'], NAMES.map(n => [n, ...['東京→横浜', '東京→大阪'].map(l => { const x = U.find(x => x.profile === n && x.kind === 'route' && x.label === l); return `${x.click} ms（取得 ${x.fetch} / サーバー ${x.server}）`; })]));

o.push('<!-- TABLES:SHOTS -->');
const img = (f, w) => `<a href="img/${f}.jpg"><img src="img/${f}.jpg" width="${w}"></a>`;
for (const n of NAMES) {
  const w = n === 'スマホ相当' ? 120 : 260;
  o.push(`### ${n}`, '');
  t(['ズーム', '30 分', '120 分', '480 分', 'パン後（480 分）'], [8, 10, 13].map(z => [z, ...[30, 120, 480].map(lim => { const x = find(n, z, 'reach', lim); return `${img(x.img, w)}<br>${s1(x.click)}`; }), img(find(n, z, 'pan').img, w)]));
  const rs = U.filter(x => x.profile === n && x.kind === 'route');
  t(rs.map(x => x.label), [rs.map(x => `${img(x.img, w)}<br>${x.click} ms`)]);
}
fs.writeFileSync('tables_gen.md', o.join('\n'));
// head.md / tail.md の <!-- TABLES:xxx --> を差し込む
const tpl = fs.readFileSync('doc_tpl.md', 'utf8');
const parts = o.join('\n').split(/<!-- TABLES:(\w+) -->\n/).slice(1);
const map = {}; for (let i = 0; i < parts.length; i += 2) map[parts[i]] = parts[i + 1];
fs.writeFileSync(`${OUT}/README.md`, tpl.replace(/<!-- TABLES:(\w+) -->/g, (_, k) => map[k].trim()));
console.log('written');
