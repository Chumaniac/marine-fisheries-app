const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

async function main() {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  try {
    const page = await browser.newPage();
    await page.goto(process.env.QUALITY_URL);
    const account = page.locator('#session-account');
    const waitForAccount = (name) => account.filter({ hasText: name }).waitFor();
    await page.locator('#connect').click();
    await waitForAccount('捕捞');
    await page.locator('#organization').selectOption('Org2MSP');
    assert.match(await account.innerText(), /捕捞/);
    await page.locator('#connect').click();
    await waitForAccount('加工');
    await page.reload();
    await waitForAccount('加工');
    await page.locator('#logout').click();
    await waitForAccount('未连接');
  } finally {
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
