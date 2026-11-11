// Run only against a disposable local database with no registered sites/devices.
const {chromium,expect}=require(process.env.PLAYWRIGHT_MODULE||'@playwright/test');
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const context=await browser.newContext({viewport:{width:1365,height:900}}),page=await context.newPage();
  const base=process.env.QUALITY_URL||'http://127.0.0.1:14997';
  await page.goto(base);await page.locator('#connect').click();
  await expect(page.locator('#device-prerequisite')).toContainText('先保存观测站点');
  await expect(page.locator('#device-form button')).toBeDisabled();
  await expect(page.locator('#environment-form button')).toBeDisabled();
  for(const suffix of ['a','b']){
   await page.locator('#site-id').fill('craft-site-'+suffix);await page.locator('#site-name').fill('合成站点 '+suffix);
   await page.locator('#site-lon').fill('10');await page.locator('#site-lat').fill('20');
   if(suffix==='b')await page.route('**/v1/devices',route=>route.abort('failed'),{times:1});
   await page.locator('#site-form button').click();
   await expect(page.locator('#site-form .form-feedback')).toContainText('站点已保存');
   if(suffix==='b'){
    await expect(page.locator('#site-form .form-feedback')).toContainText('无需再次提交');
    await page.locator('#load-data').click();await expect(page.locator('#load-data')).not.toHaveAttribute('aria-busy','true');
   }
   await expect(page.locator('#device-form button')).toBeEnabled();
   await page.locator('#device-site').selectOption('craft-site-'+suffix);
   await page.locator('#device-id').fill('craft-device-'+suffix);await page.locator('#device-form button').click();
   await expect(page.locator('#device-form .form-feedback')).toContainText('设备与站点已绑定');
  }
  await page.locator('#reading-device').selectOption('craft-device-b');
  await page.locator('#reading-temperature').fill('12.5');await page.locator('#reading-salinity').fill('30');await page.locator('#reading-turbidity').fill('1.5');
  await page.locator('#reading-time').fill(new Date(Date.now()-60000).toISOString().slice(0,16));
  await page.locator('#load-data').click();await expect(page.locator('#load-data')).not.toHaveAttribute('aria-busy','true');
  await expect(page.locator('#reading-device')).toHaveValue('craft-device-b');
  await page.route('**/v1/devices',route=>route.abort('failed'),{times:1});
  await page.locator('#load-data').click();
  await expect(page.locator('#environment-prerequisite')).toContainText('刷新未完成');
  await expect(page.locator('#reading-temperature')).toHaveValue('12.5');
  await expect(page.locator('#reading-device')).toHaveValue('craft-device-b');
  await page.locator('#load-data').click();await expect(page.locator('#load-data')).not.toHaveAttribute('aria-busy','true');
  await context.setOffline(true);await page.locator('#environment-form button').click();
  await expect(page.locator('#environment-form .form-feedback')).toContainText('本机');
  await expect(page.locator('#environment-form .form-feedback')).not.toContainText('已保存或');
  await expect(page.locator('#pending')).toHaveText('1');
  if(process.env.DATA_ENTRY_SCREENSHOT)await page.locator('#environment-form').screenshot({path:process.env.DATA_ENTRY_SCREENSHOT});
  await context.setOffline(false);await page.locator('#connect').click();
  // Reconnection retains the existing retry deadline; the normal cycle runs after 15s.
  await expect(page.locator('#pending')).toHaveText('0',{timeout:25000});
  const sources=await (await page.request.get(base+'/v1/source-events?siteId=craft-site-b')).json();
  expect(sources.events.filter(event=>event.eventType==='environment.reading'&&event.deviceId==='craft-device-b')).toHaveLength(1);
  const identity=await (await page.request.get(base+'/v1/me')).json();
  const disabled=await page.request.put(base+'/v1/devices/craft-device-b',{headers:{'X-Marine-Scope':identity.scopeHash},data:{siteId:'craft-site-b',enabled:false}});
  expect(disabled.ok()).toBe(true);
  await page.locator('#load-data').click();await expect(page.locator('#load-data')).not.toHaveAttribute('aria-busy','true');
  await expect(page.locator('#reading-device')).toHaveValue('');await expect(page.locator('#environment-form button')).toBeDisabled();
  await expect(page.locator('#environment-prerequisite')).toContainText('重新选择');
  await page.locator('#load-data').click();await expect(page.locator('#load-data')).not.toHaveAttribute('aria-busy','true');
  await expect(page.locator('#reading-device')).toHaveValue('');await expect(page.locator('#reading-temperature')).toHaveValue('12.5');
  await page.locator('#reading-device').selectOption('craft-device-a');await expect(page.locator('#environment-form button')).toBeEnabled();
  await page.setViewportSize({width:390,height:844});
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
  if(process.env.DATA_ENTRY_MOBILE_SCREENSHOT)await page.locator('#environment-form').screenshot({path:process.env.DATA_ENTRY_MOBILE_SCREENSHOT});
  if(process.env.AXE_MODULE){const AxeBuilder=require(process.env.AXE_MODULE).default;expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);}
  await page.locator('#logout').click();await expect(page.locator('#device-form button')).toBeDisabled();
  await expect(page.locator('#reading-device option')).toHaveCount(0);
  console.log('PASS: empty prerequisites → register sites/devices → preserve selection and input on refresh failure → offline queue → single acknowledged reading → mobile/logout; '+(process.env.AXE_MODULE?'axe: 0 violations':'axe: not run'));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
