// Drives the page in a real browser (installed Chrome) against a running app on port 5177.
//   npx -y -p playwright-core node tests/ui_check.mjs "C:\path\to\video.mp4"
import { chromium } from 'playwright-core';

const video = process.argv[2];
const base = process.argv[3] || 'http://127.0.0.1:5177';
const errors = [];
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
page.on('pageerror', e => errors.push('pageerror: ' + e.message));
// refused requests (the junk import below, a poll for a video just removed) are expected; script errors are not
page.on('console', m => { if (m.type() === 'error' && !m.text().includes('Failed to load resource')) errors.push('console: ' + m.text()); });
const step = (name, ok) => { console.log((ok ? '  PASS  ' : '  FAIL  ') + name); if (!ok) process.exitCode = 1; };

await page.goto(base);
step('page loads with no ffmpeg warning', await page.locator('#banner').isHidden());
await page.click('#go');
step('pressing Find with nothing chosen shows a message', (await page.textContent('#newErr')).includes('Choose a video'));
await page.fill('#path', video);
await page.fill('#min_len', '6'); await page.fill('#max_len', '20'); await page.fill('#count', '4');
await page.selectOption('#model', 'base');
await page.click('#go');
await page.waitForSelector('#work:not(.hidden)', { timeout: 600000 });
const n = await page.locator('.m').count();
step('moments are listed after processing', n >= 1);
await page.locator('.m .pick').first().check();
await page.locator('.m .h').first().fill('Test hook');
await page.locator('.m .prev').first().click();
await page.waitForTimeout(1500);
step('preview plays in the player', await page.evaluate(() => document.getElementById('player').currentTime > 0));
// import keeps the tick and the typed hook
await page.fill('#importBox', '[{"start": 2, "end": 12, "hook": "Imported pick"}]');
await page.click('#importBtn');
await page.waitForFunction(k => document.querySelectorAll('.m').length === k + 1, n);
const kept = await page.evaluate(() => [...document.querySelectorAll('.m')].filter(e => e.querySelector('.pick').checked).map(e => e.querySelector('.h').value));
step('ticks and typed hooks survive adding Claude picks', kept.length === 1 && kept[0] === 'Test hook');
await page.fill('#importBox', 'nonsense');
await page.click('#importBtn');
await page.waitForFunction(() => document.getElementById('importErr').textContent.length > 0);
step('junk in the import box shows a message', true);
await page.click('#exportBtn');
await page.waitForSelector('#clipsCard:not(.hidden)', { timeout: 600000 });
await page.waitForFunction(() => document.getElementById('exportMsg').textContent.startsWith('Done'), null, { timeout: 600000 });
step('export finishes and the clip appears', (await page.locator('#clips video').count()) === 1 && (await page.textContent('#exportErr')) === '');
await page.screenshot({ path: 'tests/_work/ui.png', fullPage: true });
// remove the project through the page
page.on('dialog', d => d.accept());
await page.locator('#projects a.del').first().click();
await page.waitForFunction(() => document.getElementById('work').classList.contains('hidden'));
step('removing the video clears the page', (await page.locator('#projects a.del').count()) === 0);
step('no JavaScript errors', errors.length === 0);
if (errors.length) console.log(errors.join('\n'));
await browser.close();
