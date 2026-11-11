const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1365,height:900}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.QUALITY_URL);
  await page.locator('#connect').click();
  await page.locator('#batch-list option').first().waitFor({state:'attached'});
  const identity=await (await page.request.get(new URL('/v1/me',page.url()).href)).json();
  await page.evaluate(identity=>new Promise((resolve,reject)=>{
   const open=indexedDB.open('marine-local-v1',2);open.onerror=()=>reject(open.error);open.onsuccess=()=>{
    const db=open.result,tx=db.transaction('queue','readwrite'),store=tx.objectStore('queue');
    const item={requestId:'DIAG_PRIVATE_REQUEST',scopeHash:identity.scopeHash,tenant:identity.tenant,subject:identity.subject,path:'/v1/batches/DIAG_PRIVATE_BATCH/transitions',body:{note:'DIAG_PRIVATE_BODY'},file:new Blob(['DIAG_PRIVATE_FILE']),lastMessage:'DIAG_PRIVATE_ERROR',createdAt:Date.now(),dependencies:['DIAG_PRIVATE_BATCH'],blocked:true,lastStatus:409,attempt:2};
    store.put(item);store.put({...item,requestId:'DIAG_OTHER_REQUEST',tenant:'DIAG_PRIVATE_OTHER_TENANT'});
    tx.oncomplete=()=>{db.close();resolve();};tx.onerror=()=>reject(tx.error);
   };
  }),identity);
  await page.locator('[data-workspace-link=operations]').click();
  await page.locator('.queue-panel').evaluate(element=>element.open=true);
  const diagnosticDownload=page.waitForEvent('download');await page.locator('#queue-diagnostics').click();
  const diagnostic=require('node:fs').readFileSync(await (await diagnosticDownload).path(),'utf8'),report=JSON.parse(diagnostic);
  if(report.counts.all!==1||report.details[0].failureClass!=='conflict'||diagnostic.includes('DIAG_PRIVATE_'))throw Error('Local queue diagnostic scope or privacy failure');
  await page.evaluate(()=>new Promise((resolve,reject)=>{const open=indexedDB.open('marine-local-v1',2);open.onsuccess=()=>{const db=open.result,tx=db.transaction('queue','readwrite');for(const id of ['DIAG_PRIVATE_REQUEST','DIAG_OTHER_REQUEST'])tx.objectStore('queue').delete(id);tx.oncomplete=()=>{db.close();resolve();};tx.onerror=()=>reject(tx.error);};}));
  await page.locator('[data-workspace-link=trace]').click();
  await page.locator('#batch-list').selectOption('synthetic-pages');
  let completeOld;
  await page.route('**/v1/batches/synthetic-pages/ledger-verify',route=>new Promise(resolve=>{completeOld=async()=>{await route.fulfill({json:{verified:true}});resolve();};}),{times:1});
  await page.locator('#trace').click();
  await page.waitForFunction(()=>document.querySelectorAll('#evidence-list option').length===100);
  for(let i=0;i<100&&!completeOld;i++)await new Promise(resolve=>setTimeout(resolve,10));
  if(!completeOld)throw Error('old verification did not start');
  await page.locator('#batch-list').selectOption('synthetic-other');
  await completeOld();
  await page.waitForFunction(()=>!document.getElementById('trace').hasAttribute('aria-busy'));
  if((await page.locator('#ledger-result').textContent()).includes('一致'))throw Error('late verification overwrote a new batch');
  await page.locator('#batch-list').selectOption('synthetic-pages');
  await page.locator('#trace').click();
  await page.waitForFunction(()=>document.querySelectorAll('#evidence-list option').length===100);
  for(const kind of ['evidence','issues','ledger']){
   for(let i=0;i<2;i++){
    await page.locator('#trace-more-'+kind).click();
    await page.waitForFunction(id=>!document.getElementById(id).hasAttribute('aria-busy'),'trace-more-'+kind);
   }
   if(!(await page.locator('#trace-more-'+kind).isDisabled()))throw Error('last trace page is not disabled');
  }
  if(await page.locator('#evidence-list option').count()!==5)throw Error('evidence cursor lost records');
  if(await page.locator('#cold-issue-list option').count()!==5)throw Error('cold-chain cursor lost records');
  if(!(await page.locator('#trace-result').textContent()).includes('205'))throw Error('global trace counts were truncated');
  await page.locator('[data-workspace-link=operations]').click();
  await page.locator('#load-operations').click();
  await page.locator('#outbox-list option').first().waitFor({state:'attached'});
  await page.locator('#incident-note').fill('Reviewed synthetic outage; preserve original request');
  await page.locator('#retry-outbox').click();
  await page.waitForFunction(()=>!document.getElementById('retry-outbox').hasAttribute('aria-busy'));
  if(await page.locator('#outbox-list option').count()!==0)throw Error('manual retry did not leave action-required queue');
  await page.locator('[data-workspace-link=collection]').click();
  await page.locator('#fill-synthetic').click();
  if(await page.locator('#site-id').inputValue()!=='synthetic-example-01')throw Error('sample input failed');
  if(process.env.UI_EVIDENCE)await page.screenshot({path:process.env.UI_EVIDENCE+'/marine-ui-desktop.png'});
  await page.setViewportSize({width:390,height:844});
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2)){console.log(await page.evaluate(()=>[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth+2).slice(0,12).map(e=>({tag:e.tagName,id:e.id,width:e.getBoundingClientRect().width,right:e.getBoundingClientRect().right}))));throw Error('mobile overflow');}
  await page.setViewportSize({width:320,height:844});
  if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2))throw Error('320px overflow');
  if(process.env.UI_EVIDENCE)await page.screenshot({path:process.env.UI_EVIDENCE+'/marine-ui-mobile.png'});
  if(errors.length)throw Error(errors.join(';'));
  console.log('PASS: real Chrome three trace cursors/global totals, bounded pages, manual original-request retry and mobile layout');
 }finally{await browser.close();}
})().catch(e=>{console.error(e.message);process.exit(1)});
