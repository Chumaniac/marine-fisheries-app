// Synthetic current-page metadata; filtering must not submit or lose selections.
const {chromium,expect}=require(process.env.PLAYWRIGHT_MODULE||'@playwright/test');
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];let sourceReads=0,businessWrites=0,checkingFilters=false;
  page.on('pageerror',error=>errors.push(error.message));
  page.on('request',request=>{if(checkingFilters&&request.method()==='POST')businessWrites++;});
  await page.route('**/v1/source-events?**',route=>{
   sourceReads++;const siteId=new URL(route.request().url()).searchParams.get('siteId');
   const event=(eventId,eventType,deviceId,imageAvailable)=>({eventId,eventType,deviceId,imageAvailable,siteId,occurredAt:'2026-11-04T00:00:00Z',revision:1});
   return route.fulfill({json:{events:[event('fish-photo','fish.observation','camera-alpha',true),event('environment','environment.reading','sensor-alpha',false),event('fish-other','fish.observation','camera-beta',false)],hasMore:false,nextCursor:0}});
  });
  await page.goto(process.env.QUALITY_URL+'/#data-entry-title');await page.locator('#connect').click();
  const sources=page.locator('#catch-sources');await expect(sources.locator('option')).toHaveCount(3);const reads=sourceReads;checkingFilters=true;
  await sources.selectOption(['fish-photo']);await page.locator('#catch-source-type').selectOption('environment.reading');
  await expect(sources.locator('option')).toHaveCount(2);expect(await sources.evaluate(node=>[...node.selectedOptions].map(option=>option.value))).toEqual(['fish-photo']);
  await expect(sources.locator('option[value="fish-photo"]')).toContainText('已选，筛选外');
  await page.locator('#catch-source-images').check();await expect(page.locator('#catch-source-filter-state')).toContainText('匹配 0 / 3');
  await sources.selectOption([]);await page.locator('#catch-source-query').fill('not-present');await expect(sources.locator('option')).toHaveCount(0);
  await page.locator('#catch-source-reset').click();await expect(sources.locator('option')).toHaveCount(3);
  await page.locator('#catch-source-query').fill(' CAMERA-ALPHA ');await expect(sources.locator('option')).toHaveCount(1);await expect(sources.locator('option')).toHaveAttribute('value','fish-photo');
  expect(sourceReads).toBe(reads);expect(businessWrites).toBe(0);expect(errors).toEqual([]);
  if(process.env.SOURCE_FILTER_SCREENSHOT)await page.locator('.source-filters').screenshot({path:process.env.SOURCE_FILTER_SCREENSHOT});
  await page.setViewportSize({width:390,height:844});expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  await page.locator('#logout').click();await expect(page.locator('#catch-source-query')).toHaveValue('');await expect(sources.locator('option')).toHaveCount(0);
  console.log('PASS: type/image/text filters, preserved selected sources, empty/reset, no source refetch or business write, mobile and logout');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
