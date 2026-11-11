// Isolated service only: metadata replies below are synthetic; no observation is created.
const {chromium,expect}=require(process.env.PLAYWRIGHT_MODULE||'@playwright/test');
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  for(const kind of ['late-success','late-failure','logout-and-late-failure']){
   const context=await browser.newContext({viewport:{width:1280,height:900}}),page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
   let release,started,heldOnce=false;const held=new Promise(resolve=>release=resolve),initial=new Promise(resolve=>started=resolve);
   const sources=id=>({events:id?[{eventId:id,eventType:'fish.observation',deviceId:'synthetic-device',occurredAt:'2026-10-25T00:00:00Z',imageAvailable:true,revision:'synthetic'}]:[],hasMore:false,nextCursor:0});
   await page.route('**/v1/source-events?**',async route=>{
    const site=new URL(route.request().url()).searchParams.get('siteId');
    if(site==='old-site'&&!heldOnce){heldOnce=true;started();await held;return route.fulfill(kind==='late-success'?{json:sources('image-a')}:{status:503,json:{code:'SYNTHETIC_TEMPORARY_FAILURE'}}).catch(()=>{});}
    return route.fulfill({json:sources(site==='new-site'?'image-b':null)});
   });
   try{
    await page.goto(process.env.QUALITY_URL);await page.locator('#connect').click();await expect(page.locator('#connect')).not.toHaveAttribute('aria-busy','true');
    await page.locator('#filter-site').fill('old-site');await page.locator('#load-data').click();await initial;
    if(kind.startsWith('logout')){await page.locator('#logout').click();await page.locator('#filter-site').fill('new-site');await page.locator('#connect').click();}
    else{await page.locator('#filter-site').fill('new-site');await page.locator('#refresh').click();}
    await expect(page.locator('#source-image-list option[value="image-b"]')).toHaveCount(1);release();await expect(page.locator('#load-data')).not.toHaveAttribute('aria-busy','true');
    await expect(page.locator('#filter-site')).toHaveValue('new-site');await expect(page.locator('#source-image-list option[value="image-b"]')).toHaveCount(1);
    await expect(page.locator('#source-image-list option[value="image-a"]')).toHaveCount(0);await expect(page.locator('#environment-prerequisite')).not.toContainText('刷新未完成');expect(errors).toEqual([]);
    if(process.env.SOURCE_REQUEST_SCREENSHOT&&kind==='late-success'){await page.locator('#source-image-list').scrollIntoViewIfNeeded();await page.screenshot({path:process.env.SOURCE_REQUEST_SCREENSHOT});}
    console.log('PASS: '+kind+' cannot replace current site sources or failure state');
   }finally{release();await context.close();}
  }
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
