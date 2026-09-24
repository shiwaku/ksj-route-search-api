// 480 分・auto（major）の README 用キャプチャ（docs/img/reach_480_major_z6.png）
// 使い方: API を --port 8001、web を --port 5174 で立て、 node web/capture_480.mjs <出力dir> 6.3 36.6 138.6 reach_480_major_z6
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);   // playwright は別途 npm i -D playwright
const { chromium } = require('playwright');
const [OUT, ZOOM, LAT, LON, NAME] = process.argv.slice(2);
const browser = await chromium.launch({ headless: true, args: ['--enable-unsafe-swiftshader', '--disable-web-security'] });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
page.setDefaultTimeout(120000);
const errors = [];
page.on('pageerror', e => errors.push('pageerror: ' + e.message));
page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
await page.route('http://localhost:8000/**', r => r.continue({ url: r.request().url().replace('localhost:8000', 'localhost:8001') }));
await page.goto(`http://localhost:5174/#${ZOOM}/${LAT}/${LON}`);
await page.getByText('API 準備完了').waitFor();
await page.waitForFunction(() => window.__map && window.__map.loaded());
await page.waitForTimeout(1500);
await page.locator('select').selectOption('480');
// 東京駅を地図座標→画面座標に変換してクリック
const pt = await page.evaluate(() => { const p = window.__map.project([139.7671, 35.6812]); return { x: p.x, y: p.y }; });
const box = await page.locator('.maplibregl-canvas').boundingBox();
await page.mouse.click(box.x + pt.x, box.y + pt.y);
await page.getByText('到達リンク').waitFor();
await page.waitForFunction(() => !document.body.innerText.includes('計算中'));
await page.waitForFunction(() => window.__map.loaded() && !window.__map.isMoving());
await page.waitForTimeout(3000);   // SwiftShader の描画待ち
console.log((await page.locator('dl').first().innerText()).replace(/\n/g, ' | '));
// DB を止めた環境で撮るため、ブックマーク欄の接続エラー文だけ隠す（到達圏の描画とは無関係）
await page.evaluate(() => { for (const el of document.querySelectorAll('p, div')) if (el.children.length === 0 && el.textContent.startsWith('データベースに接続できません')) el.remove(); });
await page.screenshot({ path: `${OUT}/${NAME}.png` });
console.log(errors.length ? '⚠ ' + errors.join('\n') : 'ブラウザエラー: なし');
await browser.close();
