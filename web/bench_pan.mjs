// パン時の重さを測る: 5173/8000 が動いている状態で  cd web && node bench_pan.mjs
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);   // playwright は別途 npm i -D playwright
const { chromium } = require('playwright');
const browser = await chromium.launch({ headless: true, args: ['--enable-unsafe-swiftshader'] });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
page.setDefaultTimeout(120000);
const net = { roads: [0, 0], gsi: [0, 0] };   // [件数, bytes]
page.on('response', async r => {
  const u = r.url(); let n = 0;
  try { n = parseInt(r.headers()['content-length'] || '0') || (await r.body()).length; } catch {}
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

console.log('--- z8.2 関東（到達圏なし）');
await pan('A 全部あり');
await pan('A 全部あり（2 回目・キャッシュ後）');
await vis('roads-base', 'none');           await pan('B roads-base（薄い全道路）なし');
for (const l of roadLayers) await vis(l, 'none'); await pan('C 道路レイヤ全部なし（背景のみ）');
for (const l of roadLayers) await vis(l, 'visible');
// 地理院背景を消して道路だけ
const bg = await page.evaluate(() => window.__map.getStyle().layers.filter(l => l.source === 'v').map(l => l.id));
await page.evaluate(ids => ids.forEach(id => window.__map.setLayoutProperty(id, 'visibility', 'none')), bg);
await pan('D 背景なし・道路レイヤのみ');
await page.evaluate(ids => ids.forEach(id => window.__map.setLayoutProperty(id, 'visibility', 'visible')), bg);

console.log('--- z8.2 到達圏 120 分を載せた状態');
const box = await page.locator('.maplibregl-canvas').boundingBox();
const pt = await page.evaluate(() => { const p = window.__map.project([139.7671, 35.6812]); return { x: p.x, y: p.y }; });
await page.mouse.click(box.x + pt.x, box.y + pt.y);
await page.getByText('到達リンク').waitFor();
await page.waitForFunction(() => !document.body.innerText.includes('計算中'));
await page.waitForTimeout(3000);
console.log('  ' + (await page.locator('dl').first().innerText()).replace(/\n/g, ' | '));
await pan('E 全部あり＋到達圏');
await vis('roads-base', 'none'); await pan('F roads-base なし＋到達圏'); await vis('roads-base', 'visible');

console.log('--- z11 都心（到達圏あり）');
await page.evaluate(() => window.__map.jumpTo({ center: [139.75, 35.68], zoom: 11 }));
await page.waitForFunction(() => window.__map.loaded()); await page.waitForTimeout(1500);
await pan('G z11 全部あり＋到達圏');
await vis('roads-base', 'none'); await pan('H z11 roads-base なし＋到達圏');
await browser.close();
