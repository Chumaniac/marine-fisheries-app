// Use tools/quality_browser_fixture.py: synthetic source pages, real authenticated download of its retained test image.
const {chromium,expect}=require(process.env.PLAYWRIGHT_MODULE||'@playwright/test');
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const context=await browser.newContext({viewport:{width:1280,height:900},acceptDownloads:true}),page=await context.newPage(),errors=[];
  page.on('pageerror',error=>errors.push(error.message));let removed=false,sourceReads=0;
  const event=id=>({eventId:id,eventType:'fish.observation',deviceId:'synthetic-device',occurredAt:'2026-10-24T00:00:00Z',imageAvailable:true,revision:1});
  await page.route('**/v1/source-events?**',route=>{
   sourceReads++;const after=Number(new URL(route.request().url()).searchParams.get('before')||0);
   return route.fulfill({json:{events:(after?['image-c']:removed?['image-a']:['image-a','image-b']).map(event),hasMore:!after,nextCursor:after?0:10}});
  });
  await page.goto(process.env.QUALITY_URL);await page.locator('#connect').click();
  await expect(page.locator('#source-image-list option[value="image-b"]')).toHaveCount(1);
  await page.locator('#source-image-list').selectOption('image-b');
  await page.locator('#load-data').click();await expect(page.locator('#load-data')).not.toHaveAttribute('aria-busy','true');
  await expect(page.locator('#source-image-list')).toHaveValue('image-b');
  removed=true;await page.locator('#load-data').click();await expect(page.locator('#load-data')).not.toHaveAttribute('aria-busy','true');
  await expect(page.locator('#source-image-list')).toHaveValue('');await expect(page.locator('#download-observation')).toBeDisabled();
  await page.locator('#source-image-list').selectOption('image-a');const before=sourceReads;
  await page.locator('#source-more').click();await expect(page.locator('#source-more')).not.toHaveAttribute('aria-busy','true');
  expect(sourceReads).toBe(before);await expect(page.locator('#source-image-list')).toHaveValue('image-a');
  await page.locator('#source-image-list').selectOption('');await page.locator('#source-more').click();
  await expect(page.locator('#source-image-list option[value="image-c"]')).toHaveCount(1);
  await expect(page.locator('#source-image-list')).toHaveValue('');await expect(page.locator('#download-observation')).toBeDisabled();
  await page.locator('#source-image-list').selectOption('image-c');
  const download=page.waitForEvent('download');await page.locator('#download-observation').click();const received=await download;expect(received.url()).toBe(new URL('/v1/observations/image-c/evidence',process.env.QUALITY_URL).href);const bytes=require('node:fs').readFileSync(await received.path());expect(bytes.toString('base64')).toBe('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jXioAAAAASUVORK5CYII=');
  expect(errors).toEqual([]);
  await page.setViewportSize({width:390,height:844});await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  if(process.env.SOURCE_IMAGE_SCREENSHOT)await page.locator('#data-entry-title').scrollIntoViewIfNeeded().catch(()=>{});
  if(process.env.SOURCE_IMAGE_SCREENSHOT)await page.screenshot({path:process.env.SOURCE_IMAGE_SCREENSHOT});
  await page.locator('#logout').click();await expect(page.locator('#download-observation')).toBeDisabled();
  console.log('PASS: image selection survives same-page refresh; removed selection clears; pagination requires explicit deselection; only chosen image downloads; mobile/logout');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
