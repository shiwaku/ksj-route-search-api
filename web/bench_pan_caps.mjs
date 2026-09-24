// 線端（cap/join）の組み合わせでパンの重さを比べる: cd web && node bench_pan_caps.mjs
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);   // playwright は別途 npm i -D playwright
const { chromium } = require('playwright');
const browser = await chromium.launch({ headless: true, args: ['--enable-unsafe-swiftshader'] });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
// 巨大な console 出力で Playwright が落ちる（ERR_STRING_TOO_LONG）ので、ページ側で切り詰めて正体だけ出す
await page.addInitScript(() => { for (const k of ['log','warn','error','info','debug']) { const f = console[k]; console[k] = (...a) => { const s = a.map(x => { try { return typeof x === 'string' ? x : JSON.stringify(x); } catch { return String(x); } }).join(' '); if (s.length > 20000) f.call(console, 'HUGE ' + k + ' len=' + s.length + ' ' + s.slice(0, 300)); else f.apply(console, a); }; } });
page.on('console', m => { if (m.text().startsWith('HUGE')) console.log('  ⚠ ' + m.text().slice(0, 400)); });
page.setDefaultTimeout(120000);
const net = { roads: [0, 0], gsi: [0, 0] };   // [件数, bytes]
page.on('response', async r => {
  const u = r.url(); let n = 0;
  try { n = parseInt(r.headers()['content-length'] || '0'); } catch {}
  if (u.includes('roads_nationwide.pmtiles')) { net.roads[0]++; net.roads[1] += n; }
  else if (u.includes('gsi.go.jp') || u.includes('cyberjapan')) { net.gsi[0]++; net.gsi[1] += n; }
});
const reset = () => { net.roads = [0, 0]; net.gsi = [0, 0]; };
const fmt = () => `道路タイル ${net.roads[0]} 件 ${(net.roads[1] / 1e6).toFixed(1)} MB / 地理院 ${net.gsi[0]} 件 ${(net.gsi[1] / 1e6).toFixed(1)} MB`;

await page.goto('http://localhost:5173/#8.2/35.83/139.99');
await page.getByText('API 準備完了').waitFor();
await page.waitForFunction(() => window.__map && window.__map.loaded());
await page.waitForTimeout(2000);

// 1 回のパン計測: 東へ 600px を 1.5 秒で動かし、rAF でフレーム間隔を記録。終わったら元に戻す
async function pan(label) {
  await page.waitForFunction(() => window.__map.loaded());
  reset();
  const r = await page.evaluate(async () => {
    const m = window.__map; const dts = []; let last = performance.now(); let run = true;
    const tick = () => { const t = performance.now(); dts.push(t - last); last = t; if (run) requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
    const t0 = performance.now();
    await new Promise(res => { m.once('moveend', res); m.panBy([600, 0], { duration: 1500 }); });
    await new Promise(res => { const f = () => m.loaded() ? res() : setTimeout(f, 50); f(); });
    const total = performance.now() - t0; run = false;
    dts.sort((a, b) => a - b);
    const p = q => dts[Math.floor(dts.length * q)].toFixed(0);
    return { frames: dts.length, med: p(0.5), p90: p(0.9), max: dts[dts.length - 1].toFixed(0), total: total.toFixed(0) };
  });
  await page.evaluate(() => window.__map.panBy([-600, 0], { duration: 0 }));
  await page.waitForFunction(() => window.__map.loaded());
  await page.waitForTimeout(500);
  console.log(`${label.padEnd(34)} | フレーム中央値 ${r.med} ms / p90 ${r.p90} ms / 最大 ${r.max} ms | 動き始め〜描画完了 ${r.total} ms | ${fmt()}`);
}
const vis = (id, v) => page.evaluate(([id, v]) => window.__map.setLayoutProperty(id, 'visibility', v), [id, v]);
const roadLayers = ['roads-base', 'roads-reach-casing', 'roads-reach'];


const capOf = (cap) => page.evaluate(cap => { for (const id of ['roads-reach-casing', 'roads-reach']) { window.__map.setLayoutProperty(id, 'line-cap', cap); window.__map.setLayoutProperty(id, 'line-join', cap === 'butt' ? 'miter' : cap === 'square' ? 'bevel' : 'round'); } }, cap);

const setLJ = (cap, join) => page.evaluate(([cap, join]) => { for (const id of ['roads-reach-casing', 'roads-reach']) { window.__map.setLayoutProperty(id, 'line-cap', cap); window.__map.setLayoutProperty(id, 'line-join', join); } }, [cap, join]);
console.log('--- z8.2 到達圏なし・cap/join の組み合わせ（各 2 回）');
for (const [cap, join] of [['butt','miter'],['square','miter'],['square','bevel'],['square','round'],['round','round'],['butt','miter']]) {
  await setLJ(cap, join); await pan(cap + '/' + join + ' 1'); await pan(cap + '/' + join + ' 2');
}
await browser.close();
