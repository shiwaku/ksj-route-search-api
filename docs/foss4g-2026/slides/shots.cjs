// スライド用の画面: 会場（トナリエ北広島）起点（build.py が使う shots/ を撮る）
//   node shots.cjs <出力dir>   ※ playwright が要る。API は 127.0.0.1:8001・画面は npm run dev（5173）で起動しておく
//   （8000 が他で塞がっていたので、/api を 8001 に付け替えている）
const { chromium } = require('playwright');
const OUT = process.argv[2];
const VENUE = [141.562927, 42.978001], SAPPORO = [141.3508, 43.0687];
(async () => {
  const browser = await chromium.launch({ headless: true, args: ['--use-angle=d3d11', '--enable-gpu'] });
  const page = await browser.newPage({ viewport: { width: 1600, height: 900 }, deviceScaleFactor: 1.5 });
  page.setDefaultTimeout(120000);
  await page.route('**/api/**', r => r.continue({ url: r.request().url().replace(/^http:\/\/localhost:5173\/api/, 'http://127.0.0.1:8001') }));
  const idle = async () => { await page.waitForFunction(() => !document.body.innerText.includes('計算中')); await page.waitForFunction(() => window.__map.areTilesLoaded() && !window.__map.isMoving()); await page.waitForTimeout(2500); };
  const px = async ll => page.evaluate(ll => { const p = window.__map.project(ll); const r = window.__map.getCanvas().getBoundingClientRect(); return [r.left + p.x, r.top + p.y]; }, ll);
  const go = async hash => { await page.evaluate(h => { location.hash = h; }, hash); await page.waitForTimeout(800); await idle(); };
  await page.goto('http://localhost:5173/#10/42.978/141.563');
  await page.getByText('API 準備完了').waitFor(); await idle();
  // 120 分 z10
  let [x, y] = await px(VENUE); await page.mouse.click(x, y); await page.getByText('到達リンク').waitFor(); await idle();
  await page.screenshot({ path: `${OUT}/venue_120_z10.png` });
  console.log('120:', (await page.locator('dl').first().innerText()).replace(/\n/g, ' | '));
  // 近寄り z12
  await go('12/42.978/141.563'); await page.screenshot({ path: `${OUT}/venue_120_z12.png` });
  // 480 分 z7
  await go('7/43.35/142.75');
  await page.locator('select').selectOption('480'); await idle();
  await page.screenshot({ path: `${OUT}/venue_480_z7.png` });
  console.log('480:', (await page.locator('dl').first().innerText()).replace(/\n/g, ' | '));
  // 経路 会場 → 札幌駅
  await page.locator('select').selectOption('120'); await idle();
  await page.getByRole('button', { name: '経路探索' }).click();
  await go('11/43.03/141.47');
  [x, y] = await px(VENUE); await page.mouse.click(x, y);
  [x, y] = await px(SAPPORO); await page.mouse.click(x, y);
  await page.getByText('距離').waitFor(); await idle();
  await page.screenshot({ path: `${OUT}/route_venue_sapporo.png` });
  console.log('route:', (await page.locator('dl').first().innerText()).replace(/\n/g, ' | '));
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
