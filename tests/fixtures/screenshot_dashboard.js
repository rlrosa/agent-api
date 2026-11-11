const { chromium } = require('/home/ubuntu/rondeau-landing/node_modules/playwright');
const fs = require('fs');

async function run() {
  const browser = await chromium.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });

  // 1. Direct dashboard (:18090/dashboard) - trusted loopback, full admin
  const page1 = await browser.newPage({ viewport: { width: 1400, height: 1200 } });
  const logs1 = [];
  page1.on('console', msg => logs1.push(`[${msg.type()}] ${msg.text()}`));
  page1.on('pageerror', err => logs1.push(`[PAGE_ERROR] ${err.message}`));

  console.log('Navigating to http://127.0.0.1:18090/dashboard ...');
  await page1.goto('http://127.0.0.1:18090/dashboard', { waitUntil: 'networkidle' });
  await page1.waitForTimeout(2000);
  await page1.screenshot({ path: '/tmp/prl-rondeau/members/worker/evidence/33-dashboard.png', fullPage: true });
  console.log('Saved 33-dashboard.png');
  console.log('Direct Dashboard Console Logs:', logs1);

  // 2. Prefixed dashboard (:18091/prefix/dashboard) - proxy with stats key only, admin controls hidden
  const page2 = await browser.newPage({ viewport: { width: 1400, height: 1200 } });
  const logs2 = [];
  page2.on('console', msg => logs2.push(`[${msg.type()}] ${msg.text()}`));
  page2.on('pageerror', err => logs2.push(`[PAGE_ERROR] ${err.message}`));

  console.log('Navigating to http://127.0.0.1:18091/prefix/dashboard ...');
  await page2.goto('http://127.0.0.1:18091/prefix/dashboard', { waitUntil: 'networkidle' });
  await page2.waitForTimeout(2000);
  await page2.screenshot({ path: '/tmp/prl-rondeau/members/worker/evidence/33-dashboard-prefixed.png', fullPage: true });
  console.log('Saved 33-dashboard-prefixed.png');
  console.log('Prefixed Dashboard Console Logs:', logs2);

  await browser.close();

  // Filter out any benign info logs, check if any ERROR exists
  const errors1 = logs1.filter(l => l.startsWith('[error]') || l.startsWith('[PAGE_ERROR]'));
  const errors2 = logs2.filter(l => l.startsWith('[error]') || l.startsWith('[PAGE_ERROR]'));
  if (errors1.length > 0 || errors2.length > 0) {
    console.error('Console errors encountered:', { errors1, errors2 });
    process.exit(1);
  }
}

run().catch(err => {
  console.error('Screenshot run failed:', err);
  process.exit(1);
});
