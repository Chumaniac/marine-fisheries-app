// Real local session, synthetic dashboard/processing responses; no field data or model.
const {chromium,expect}=require(process.env.PLAYWRIGHT_MODULE||'@playwright/test');
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];let writes=0,payload;
  const batch=(batchId,changes={})=>({batchId,speciesId:'saithe',areaCode:'synthetic',ownerMSP:'Org2MSP',state:'LANDED',revision:2,massGrams:500,availableMassGrams:500,chainStatus:'LOCAL_ONLY',...changes});
  let rows=[batch('parent-a'),batch('parent-b')];
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/v1/dashboard?**',async route=>{const response=await route.fetch(),data=await response.json();await route.fulfill({response,json:{...data,batches:rows,hasMoreBatches:false,hasPreviousBatches:false,nextBatchCursor:'',previousBatchCursor:''}})});
  await page.route('**/v1/batch-transforms',route=>{writes++;payload=route.request().postDataJSON();return route.fulfill({json:{outputs:['synthetic-output'],lossGrams:0}})});
  await page.goto(process.env.QUALITY_URL+'/#transform-form');await expect(page.locator('#organization')).toBeEnabled();await page.locator('#organization').selectOption('Org2MSP');await page.locator('#connect').click();
  const parents=page.locator('#transform-parents');await expect(parents.locator('option')).toHaveCount(2);await parents.selectOption(['parent-a','parent-b']);
  await page.locator('#output-id').fill('synthetic-output');await page.locator('#output-mass').fill('1000');await page.locator('#loss-mass').fill('0');
  rows=[batch('parent-a'),batch('parent-b',{localConflict:true})];await page.locator('#refresh').click();
  await expect(parents.locator('option[value="parent-b"]')).toHaveJSProperty('disabled',true);expect(await parents.evaluate(node=>[...node.selectedOptions].map(option=>option.value))).toEqual(['parent-a','parent-b']);
  await page.locator('#transform-form button').last().click();await expect(page.locator('#transform-form .form-feedback')).toContainText('冲突');expect(writes).toBe(0);
  rows=[batch('parent-a')];await page.locator('#refresh').click();await expect(parents.locator('option[value="parent-b"]')).toContainText('不可用');
  await page.locator('#transform-form button').last().click();expect(writes).toBe(0);await expect(page.locator('#output-mass')).toHaveValue('1000');
  await page.locator('#transform-clear-parents').click();expect(await parents.evaluate(node=>node.selectedOptions.length)).toBe(0);
  rows=[batch('parent-a'),batch('parent-b',{ownerMSP:'Org1MSP'})];await page.locator('#refresh').click();
  await parents.evaluate(node=>{for(const option of node.options)option.selected=true;const option=document.createElement('option');option.value='parent-b';option.selected=true;node.append(option)});
  await page.locator('#transform-form button').last().click();await expect(page.locator('#transform-form .form-feedback')).toContainText('机构');expect(writes).toBe(0);
  rows=[batch('parent-a'),batch('parent-b')];await page.locator('#refresh').click();await parents.selectOption(['parent-a','parent-b']);
  await page.locator('#output-mass').fill('700');await page.locator('#transform-form button').last().click();await expect(page.locator('#transform-form .form-feedback')).toContainText('必须相等');expect(writes).toBe(0);
  await page.locator('#output-mass').fill('1000');await page.locator('#transform-form button').last().click();await expect.poll(()=>writes).toBe(1);expect(payload.parents).toEqual({'parent-a':2,'parent-b':2});
  await page.setViewportSize({width:390,height:844});expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  if(process.env.TRANSFORM_SCREENSHOT)await page.locator('#transform-form').screenshot({path:process.env.TRANSFORM_SCREENSHOT});
  await page.locator('#logout').click();await expect(parents.locator('option')).toHaveCount(0);expect(errors).toEqual([]);
  console.log('PASS: conflict/missing/cross-owner sources block before writes, choices and input survive refresh, explicit clear, conservation, normal multi-parent request, mobile/logout');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
