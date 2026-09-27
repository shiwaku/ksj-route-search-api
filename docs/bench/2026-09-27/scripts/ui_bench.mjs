// 画面（本番）の計測: node ui_bench.mjs [プロファイル名...]
// 本番ビルドには window.__map が無いので、クリック・表示値（API / 描画）・ネットワークの timing だけで測る
// 各ズームは新しいコンテキスト（ブラウザキャッシュなし）で開く
import { chromium } from 'playwright';
import fs from 'node:fs';
const URL = 'https://ksj-route-search.shi-works-worker.workers.dev/';
const GPU = ['--use-angle=d3d11', '--enable-gpu', '--ignore-gpu-blocklist'];
const NOGPU = ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--disable-gpu'];
const DESK = { viewport: { width: 1400, height: 900 } };
const MOBILE = { viewport: { width: 390, height: 844 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true };
// 回線: 下り 9 Mbps / 上り 1.5 Mbps / 往復 150 ms（モバイル回線のやや悪いとき相当）
const NET4G = { offline: false, latency: 150, downloadThroughput: 9e6 / 8, uploadThroughput: 1.5e6 / 8 };
const PROFILES = {
  'GPU': { args: GPU, ctx: DESK, cpu: 1 },
  'GPUなし': { args: NOGPU, ctx: DESK, cpu: 1 },
  'CPU4倍遅い': { args: GPU, ctx: DESK, cpu: 4 },
  '回線遅い': { args: GPU, ctx: DESK, cpu: 1, net: NET4G },
  'スマホ相当': { args: GPU, ctx: MOBILE, cpu: 4, net: NET4G },
};
const ZOOMS = [8, 10, 13];
const LIMITS = ['30', '120', '480'];
const CENTER = [139.7671, 35.6812];   // 東京駅
const SHOTS = 'C:/Users/yshiw/Documents/GIS/ksj-route-search-api/docs/bench/2026-09-27/img';
fs.mkdirSync(SHOTS, { recursive: true });
// 計測が終わってから撮る（計測値には入らない）。容量を抑えるため JPEG・CSS ピクセル
const shot = (p, name) => p.screenshot({ path: `${SHOTS}/${name}.jpg`, type: 'jpeg', quality: 70, scale: 'css' });
const names = process.argv.slice(2).length ? process.argv.slice(2) : Object.keys(PROFILES);

// web メルカトル（MapLibre のタイル 512px）で、中心・ズームからの画面上のずれ
const merc = ([lng, lat], z) => { const s = 512 * 2 ** z; const y = Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360)); return [(lng + 180) / 360 * s, (1 - y / Math.PI) / 2 * s]; };
const offset = (p, c, z) => { const a = merc(p, z), b = merc(c, z); return [a[0] - b[0], a[1] - b[1]]; };
const unmerc = ([x, y], z) => { const s = 512 * 2 ** z; return [x / s * 360 - 180, (2 * Math.atan(Math.exp(Math.PI * (1 - 2 * y / s))) - Math.PI / 2) * 180 / Math.PI]; };
// クリックする画面上の位置（ビューポートに対する比）。スマホはパネルが上半分を覆うので下寄り
const clickFrac = prof => prof.ctx.isMobile ? [0.5, 0.78] : [0.5, 0.5];
// 点 ll が画面の (fx, fy) に来るような中心
const centerFor = (ll, z, prof) => { const [fx, fy] = clickFrac(prof); const v = prof.ctx.viewport; const m = merc(ll, z); return unmerc([m[0] - (fx - 0.5) * v.width, m[1] - (fy - 0.5) * v.height], z); };
const hashFor = (ll, z, prof) => { const c = centerFor(ll, z, prof); return `#${z}/${c[1].toFixed(5)}/${c[0].toFixed(5)}`; };

const cdpOf = new Map();
async function openPage(b, prof, hash) {
  const ctx = await b.newContext(prof.ctx);
  const p = await ctx.newPage(); p.setDefaultTimeout(240000);
  const cdp = await ctx.newCDPSession(p); cdpOf.set(p, cdp);
  if (prof.cpu > 1) await cdp.send('Emulation.setCPUThrottlingRate', { rate: prof.cpu });
  if (prof.net) { await cdp.send('Network.enable'); await cdp.send('Network.emulateNetworkConditions', prof.net); }
  const net = { tiles: 0, tileBytes: 0, last: null, errors: [] };
  p.on('response', async r => {
    const u = r.url();
    if (u.includes('/tiles/')) { net.tiles++; net.tileBytes += Number(r.headers()['content-length'] ?? 0); }
    if (u.includes('/reachability') || u.includes('/route?')) net.last = { ...net.last, bytes: Number(r.headers()['content-length'] ?? 0), status: r.status(), ttfb: Date.now() - net.last.t0 };
  });
  // 取得時間 = リクエスト送信〜本文を受け取り終わる（CDP の回線制限もここに効く）
  const api = u => u.includes('/reachability') || u.includes('/route?');
  p.on('request', r => { if (api(r.url())) net.last = { t0: Date.now() }; });
  p.on('requestfinished', r => { if (api(r.url())) net.last.end = Date.now() - net.last.t0; });
  p.on('pageerror', e => net.errors.push(e.message));
  const t0 = Date.now();
  await p.goto(URL + hash);
  await p.getByText('API 準備完了').waitFor();
  const tReady = Date.now() - t0;
  await p.waitForLoadState('networkidle');
  const tIdle = Date.now() - t0;
  const box = await p.locator('.maplibregl-canvas').boundingBox();
  return { ctx, p, net, tReady, tIdle, box };
}

const notBusy = p => p.waitForFunction(() => !document.body.innerText.includes('計算中'));
async function clickAndWait(p, x, y) {
  const t = Date.now();
  await p.mouse.click(x, y);
  await p.getByText('計算中').waitFor({ state: 'visible', timeout: 3000 }).catch(() => {});
  await notBusy(p);
  return Date.now() - t;
}

// パン中のフレーム間隔（rAF）と、放してから落ち着く（networkidle）まで
async function pan(p, box, cdp) {
  const cx = box.x + box.width / 2, cy = box.y + box.height / 2;
  await p.evaluate(() => { window.__dts = []; let last = performance.now(); window.__run = true; const f = () => { const t = performance.now(); window.__dts.push(t - last); last = t; if (window.__run) requestAnimationFrame(f); }; requestAnimationFrame(f); });
  const t = Date.now();
  if (cdp) {   // スマホ相当: マウスのドラッグでは地図が動かないのでタッチで
    const tp = (x, y) => [{ x, y, id: 1 }];
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: tp(cx, cy) });
    for (let i = 1; i <= 30; i++) { await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: tp(cx - box.width * 0.4 * i / 30, cy - box.height * 0.2 * i / 30) }); await p.waitForTimeout(16); }
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  } else {
    await p.mouse.move(cx, cy); await p.mouse.down();
    for (let i = 1; i <= 30; i++) { await p.mouse.move(cx - box.width * 0.4 * i / 30, cy - box.height * 0.2 * i / 30); await p.waitForTimeout(16); }
    await p.mouse.up();
  }
  const tDrag = Date.now() - t;
  await p.waitForLoadState('networkidle'); await p.waitForTimeout(300);
  const tSettle = Date.now() - t;
  const d = await p.evaluate(() => { window.__run = false; return window.__dts.slice(1).sort((a, b) => a - b); });
  const q = x => Math.round(d[Math.min(d.length - 1, Math.floor(d.length * x))]);
  return { tDrag, tSettle, med: q(0.5), p90: q(0.9), max: Math.round(d.at(-1)), longFrames: d.filter(x => x > 50).length };
}

const results = [];
const b0 = {};
for (const name of names) {
  const prof = PROFILES[name];
  const b = await chromium.launch({ args: prof.args });
  console.log(`\n## ${name}`);
  for (const z of ZOOMS) {
    const { ctx, p, net, tReady, tIdle, box } = await openPage(b, prof, hashFor(CENTER, z, prof));
    if (z === ZOOMS[0]) console.log('  renderer:', await p.evaluate(() => { const gl = document.createElement('canvas').getContext('webgl2'); const e = gl?.getExtension('WEBGL_debug_renderer_info'); return e ? gl.getParameter(e.UNMASKED_RENDERER_WEBGL) : '?'; }));
    const load = { tReady, tIdle, tiles: net.tiles, tileMB: net.tileBytes / 1e6 };
    console.log(`  z${z} 開く: 操作できるまで ${tReady} ms / タイルが揃うまで ${tIdle} ms（タイル ${net.tiles} 件 ${load.tileMB.toFixed(1)} MB）`);
    results.push({ profile: name, z, kind: 'load', ...load });
    const [fx, fy] = clickFrac(prof); const cx = box.x + box.width * fx, cy = box.y + box.height * fy;
    for (const lim of LIMITS) {
      await p.locator('select').first().selectOption(lim); await notBusy(p);
      const click = await clickAndWait(p, cx, cy);
      const txt = await p.locator('dl').first().innerText();
      const m = txt.match(/API \/ 描画\s*([\d,]+) ms \/ ([\d,]+) ms/);
      const r = { profile: name, z, kind: 'reach', limit: Number(lim), click, server: Number(m?.[1].replace(/,/g, '')), paint: Number(m?.[2].replace(/,/g, '')), fetch: net.last?.end, ttfb: net.last?.ttfb, bytes: net.last?.bytes };
      r.img = `${name}_z${z}_${lim}min`; await shot(p, r.img);
      results.push(r);
      console.log(`  z${z} 到達圏 ${lim.padStart(3)} 分: クリック〜着色 ${String(click).padStart(5)} ms = 取得 ${Math.round(r.fetch)}（サーバー ${r.server}）+ 描画 ${r.paint}`);
    }
    const hash0 = await p.evaluate(() => location.hash);
    const pn = await pan(p, box, prof.ctx.isMobile ? cdpOf.get(p) : null);
    pn.moved = hash0 !== await p.evaluate(() => location.hash);
    pn.img = `${name}_z${z}_pan`; await shot(p, pn.img);
    results.push({ profile: name, z, kind: 'pan', ...pn });
    console.log(`  z${z} パン（480 分表示中）: フレーム中央値 ${pn.med} / p90 ${pn.p90} / 最大 ${pn.max} ms（50ms 超 ${pn.longFrames} 回）/ 地図が動いた ${pn.moved}`);
    if (net.errors.length) console.log('  エラー:', net.errors);
    await ctx.close();
  }
  // 経路: z7 で東京・大阪が両方見える位置から
  {
    const zc = prof.ctx.isMobile ? 5.6 : 7, mid = [137.63, 35.19];
    const { ctx, p, net, box } = await openPage(b, prof, hashFor(mid, zc, prof));
    await p.getByRole('button', { name: '経路探索', exact: true }).click();
    const [fx, fy] = clickFrac(prof);
    const pt = ll => { const [dx, dy] = offset(ll, mid, zc); return [box.x + box.width * fx + dx, box.y + box.height * fy + dy]; };
    for (const [label, a, d] of [['東京→横浜', CENTER, [139.6223, 35.4658]], ['東京→大阪', CENTER, [135.4959, 34.7025]]]) {
      const [ax, ay] = pt(a), [dx, dy] = pt(d);
      if (dx < box.x || dx > box.x + box.width || ax > box.x + box.width) { console.log(`  経路 ${label}: 画面外のため省略`); continue; }
      await p.mouse.click(ax, ay); await notBusy(p);
      const t = Date.now();
      await p.mouse.click(dx, dy);
      await p.getByText('計算中').waitFor({ state: 'visible', timeout: 3000 }).catch(() => {});
      await notBusy(p); await p.getByText('距離', { exact: true }).waitFor({ timeout: 20000 });
      const ms = Date.now() - t;
      const txt = await p.locator('dl').last().innerText();
      const server = Number(txt.match(/API\s*([\d,]+) ms/)?.[1].replace(/,/g, ''));
      const img = `${name}_route_${label.replace('→', '-')}`; await shot(p, img);
      results.push({ profile: name, kind: 'route', label, click: ms, server, fetch: net.last?.end, img });
      console.log(`  経路 ${label}: クリック〜表示 ${ms} ms（取得 ${Math.round(net.last?.end)} / サーバー ${server}）`);
    }
    await ctx.close();
  }
  await b.close();
}
fs.writeFileSync(`ui_${names.join('_')}.json`, JSON.stringify(results, null, 1));
