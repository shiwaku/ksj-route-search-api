// 画面の一気通貫テスト: 起動待ち → 到達圏クリック → 経路 2 クリック → ブックマーク保存/再表示/削除 → スクリーンショット
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);   // playwright は別途 npm i -D playwright
const { chromium } = require('playwright');
const OUT = process.argv[2];
const browser = await chromium.launch({ headless: true, args: ['--enable-unsafe-swiftshader'] });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
page.setDefaultTimeout(120000);
const errors = [];
page.on('pageerror', e => errors.push('pageerror: ' + e.message));
page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
page.on('requestfailed', r => errors.push('requestfailed: ' + r.url() + ' ' + r.failure()?.errorText));

const t0 = Date.now();
await page.goto('http://localhost:5173/');
await page.getByText('API 準備完了').waitFor();
console.log(`起動〜API 準備完了: ${Date.now() - t0} ms`);
await page.waitForTimeout(1500);   // タイル初期描画

// ① 到達圏（既定 120 分）: 地図中央（東京駅付近）をクリック
const box = await page.locator('.maplibregl-canvas').boundingBox();
const cx = box.x + box.width / 2 + 120, cy = box.y + box.height / 2;   // パネルを避けて中央右寄り
await page.mouse.click(cx, cy);
await page.getByText('到達リンク').waitFor();
await page.waitForFunction(() => !document.body.innerText.includes('計算中'));
const reachText = await page.locator('dl').first().innerText();
console.log('到達圏 120 分:\n  ' + reachText.replace(/\n/g, ' | '));
await page.screenshot({ path: `${OUT}/e2e_reach120.png` });

// ② 480 分に変更 → 自動再実行（auto で major に落ちるはず）
await page.locator('select').selectOption('480');
await page.waitForFunction(() => !document.body.innerText.includes('計算中'));
await page.waitForTimeout(500);
console.log('到達圏 480 分:\n  ' + (await page.locator('dl').first().innerText()).replace(/\n/g, ' | '));
await page.screenshot({ path: `${OUT}/e2e_reach480.png` });

// ③ 高速なし（120 分に戻して）
await page.locator('select').selectOption('120');
await page.waitForFunction(() => !document.body.innerText.includes('計算中'));
await page.getByLabel('高速道路を使う').uncheck();
await page.waitForFunction(() => !document.body.innerText.includes('計算中'));
await page.waitForTimeout(500);
console.log('到達圏 120 分・高速なし:\n  ' + (await page.locator('dl').first().innerText()).replace(/\n/g, ' | '));
await page.screenshot({ path: `${OUT}/e2e_reach120_noexp.png` });
await page.getByLabel('高速道路を使う').check();
await page.waitForFunction(() => !document.body.innerText.includes('計算中'));

// ④ ブックマーク保存 → 一覧に出る → 再表示 → 削除
await page.getByPlaceholder('地点名を入力').fill('E2E 東京駅付近');
await page.getByRole('button', { name: '保存' }).click();
await page.getByText('E2E 東京駅付近').waitFor();
console.log('ブックマーク保存: OK');
await page.getByText('E2E 東京駅付近').click();
await page.waitForFunction(() => !document.body.innerText.includes('計算中'));
console.log('ブックマークから再実行: ' + (await page.locator('dl').first().innerText()).split('\n').slice(0, 2).join(' '));
await page.getByTitle('削除').first().click();
await page.getByText('まだありません').waitFor();
console.log('ブックマーク削除: OK');

// ⑤ 経路: 東京駅付近 → 画面左下（横浜方面）
await page.getByRole('button', { name: '経路探索' }).click();
await page.mouse.click(cx, cy);
await page.mouse.click(cx - 150, cy + 250);
await page.getByText('距離').waitFor();
await page.waitForFunction(() => !document.body.innerText.includes('計算中'));
console.log('経路:\n  ' + (await page.locator('dl').first().innerText()).replace(/\n/g, ' | '));
await page.screenshot({ path: `${OUT}/e2e_route.png` });

console.log(errors.length ? '⚠ ブラウザエラー:\n' + errors.join('\n') : 'ブラウザエラー: なし');
await browser.close();
