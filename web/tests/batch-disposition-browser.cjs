const {chromium,expect}=require(process.env.PLAYWRIGHT_MODULE||'@playwright/test');
(async()=>{const browser=await chromium.launch({channel:'chrome'});try{
 const context=await browser.newContext({viewport:{width:390,height:844}}),page=await context.newPage(),base=process.env.QUALITY_URL||'http://127.0.0.1:14984';
 const writes=[];page.on('request',request=>{const url=new URL(request.url());if(url.pathname.startsWith('/v1/')&&url.pathname!=='/v1/session'&&!['GET','HEAD'].includes(request.method()))writes.push(url.pathname)});
 await page.goto(base);await page.locator('#connect').click();await expect(page.locator('#batch-list option')).not.toHaveCount(0);await page.locator('#batch-list').selectOption('synthetic-pages');
 const traceResponse=page.waitForResponse(response=>new URL(response.url()).pathname==='/v1/batches/synthetic-pages/trace');await page.locator('#trace').click();const trace=await (await traceResponse).json();
 const card=page.locator('#batch-disposition');await expect(card).toBeVisible();await expect(card).toContainText(`核对 ${trace.totalEvidence} 条已登记证据`);await expect(card).toContainText(`有 ${trace.totalLedgerCommands} 条存证操作待确认`);await expect(card).toContainText('记录还有后续页');
 const identity=await (await page.request.get(base+'/v1/me')).json();
 await page.evaluate(identity=>new Promise((resolve,reject)=>{const open=indexedDB.open('marine-local-v1',2);open.onerror=()=>reject(open.error);open.onsuccess=()=>{const db=open.result,tx=db.transaction('queue','readwrite'),store=tx.objectStore('queue'),item={requestId:'SYNTHETIC_PRIVATE_REQUEST',tenant:identity.tenant,subject:identity.subject,scopeHash:identity.scopeHash,batchId:'synthetic-pages',path:'/v1/batches/synthetic-pages/transitions',dependencies:['synthetic-pages'],createdAt:Date.now(),blocked:true,lastStatus:409,attempt:2,body:{note:'SYNTHETIC_PRIVATE_BODY'},lastMessage:'SYNTHETIC_PRIVATE_ERROR'};store.put(item);store.put({...item,requestId:'SYNTHETIC_FOREIGN',tenant:'other'});tx.oncomplete=()=>{db.close();resolve()};tx.onerror=()=>reject(tx.error);};}),identity);
 await expect(card).toContainText('本机有 1 条未确认操作',{timeout:25000});expect(await card.textContent()).not.toContain('SYNTHETIC_PRIVATE_');
 await card.getByRole('link',{name:'本机有 1 条未确认操作',exact:true}).click();await expect(page.locator('.queue-panel')).toHaveAttribute('open','');await expect(page.locator('#queue-summary')).toBeFocused();
 await card.getByRole('link',{name:`核对 ${trace.totalEvidence} 条已登记证据`,exact:true}).click();await expect(page.locator('#verify')).toBeFocused();
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
 if(process.env.AXE_MODULE){const AxeBuilder=require(process.env.AXE_MODULE).default;expect((await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze()).violations).toEqual([])}
 await card.scrollIntoViewIfNeeded();if(process.env.DISPOSITION_SCREENSHOT)await page.screenshot({path:process.env.DISPOSITION_SCREENSHOT});
 await page.locator('#batch-list').selectOption('synthetic-other');await expect(page.locator('#batch-disposition-status')).toHaveText('状态待核对');expect(await card.textContent()).not.toContain('205');
 await page.locator('#logout').click();await expect(card).toBeHidden();expect(writes).toEqual([]);
 console.log('PASS real synthetic trace → global 205-record totals → scoped blocked queue → native anchors → batch switch/logout; no business writes; mobile; '+(process.env.AXE_MODULE?'axe: 0 violations':'axe: not run'));
}finally{await browser.close()}})().catch(error=>{console.error(error);process.exitCode=1});
