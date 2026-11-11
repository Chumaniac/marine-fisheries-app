// Existing synthetic fixture only. No business writes, images, model or training calls.
const {chromium,expect}=require(process.env.PLAYWRIGHT_MODULE||'@playwright/test');
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const context=await browser.newContext({viewport:{width:1280,height:900}}),page=await context.newPage(),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(process.env.QUALITY_URL||'http://127.0.0.1:18879');
  await page.locator('#connect').click();await expect(page.locator('#connect')).not.toHaveAttribute('aria-busy','true');
  await page.locator('#filter-site').fill('quality-site');await page.locator('#quality-check').click();
  await expect(page.locator('#quality-state')).toContainText('有记录需要核对');
  await expect(page.locator('#quality-counts')).toContainText('已检查 3 条');
  await expect(page.locator('#quality-counts')).toContainText('其他物种 1 条');
  await expect(page.locator('#quality-issues')).toContainText('环境指标有缺项：1');
  await page.locator('#quality-issues a').first().click();await expect(page.locator('#environment-form')).toBeFocused();
  await page.route('**/v1/data-quality?**',route=>route.fulfill({status:503,json:{code:'SYNTHETIC_UNAVAILABLE'}}),{times:1});
  await page.locator('#quality-check').click();await expect(page.locator('#quality-state')).toContainText('检查未完成');
  await page.locator('#quality-check').click();await expect(page.locator('#quality-counts')).toContainText('已检查 3 条');
  await page.setViewportSize({width:390,height:844});await page.locator('#quality-title').scrollIntoViewIfNeeded();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  if(process.env.QUALITY_SCREENSHOT)await page.screenshot({path:process.env.QUALITY_SCREENSHOT});
  let release,started;const held=new Promise(resolve=>release=resolve),initial=new Promise(resolve=>started=resolve);
  await page.route('**/v1/data-quality?**',async route=>{const response=await route.fetch();const json=await response.json();started();await held;await route.fulfill({json}).catch(()=>{});},{times:1});
  await page.locator('#quality-check').click();await initial;
  await page.locator('#filter-site').fill('empty-quality-site');await page.locator('#quality-check').click();
  await expect(page.locator('#quality-state')).toContainText('本次范围没有记录');release();
  await expect(page.locator('#quality-state')).toContainText('本次范围没有记录');await expect(page.locator('#quality-issues li')).toHaveCount(0);
  await page.locator('#logout').click();await expect(page.locator('#quality-state')).toContainText('尚未检查');
  await expect(page.locator('#quality-counts')).toHaveText('');expect(errors).toEqual([]);
  console.log('PASS: real scoped data quality, other-species information, actionable keyboard link, retry, mobile, late-response isolation and logout; no business writes');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
