// Explicitly local synthetic fixture, with no business writes or ledger requests.
const {chromium,expect}=require(process.env.PLAYWRIGHT_MODULE||'@playwright/test');
(async()=>{const browser=await chromium.launch({channel:'chrome',headless:true});try{
 const context=await browser.newContext({viewport:{width:1280,height:960}}),page=await context.newPage(),errors=[],posts=[];
 const origin=process.env.MARINE_PREVIEW_URL;
 const session=await context.request.post(origin+'/v1/session',{headers:{'X-Requested-With':'marine-web','Origin':origin}});expect(session.status()).toBe(200);
 page.on('pageerror',error=>errors.push(error.message));page.on('request',request=>{if(request.method()==='POST')posts.push(new URL(request.url()).pathname)});
 await page.goto(origin);await page.locator('[data-workspace-link="trace"]').click();
 const selected=await page.locator('#batch-list').inputValue();await page.locator('[data-lineage-root]').fill('root');await page.locator('#lineage-load').click();
 const result=page.locator('[data-lineage-result]');await expect(result).toBeVisible();await expect(result.locator('tbody tr')).toHaveCount(3);await expect(result).toHaveAttribute('data-complete','true');
 const downstream=await context.request.get(origin+'/v1/batches/root/lineage?direction=descendants',{headers:{'X-Marine-Scope':(await (await context.request.get(origin+'/v1/me')).json()).scopeHash}});const data=await downstream.json();expect(data.nodes.map(node=>node.batchId).sort()).toEqual(['child','leaf','root']);expect(JSON.stringify(data)).not.toContain('PRIVATE_SYNTHETIC_HISTORY');
 await result.getByRole('button',{name:'只读展开 leaf',exact:true}).click();await expect(page.locator('[data-lineage-root]')).toHaveValue('leaf');await expect(result.locator('tbody tr')).toHaveCount(4);await expect(page.locator('#batch-list')).toHaveValue(selected);
 await page.locator('[data-lineage-direction]').selectOption('ancestors');await page.locator('[data-lineage-depth]').fill('1');await page.locator('#lineage-load').click();await expect(page.locator('[data-lineage-status]')).toContainText('部分谱系');await expect(result).toHaveAttribute('data-complete','false');
 await page.setViewportSize({width:390,height:844});await page.locator('#lineage-view').scrollIntoViewIfNeeded();expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);if(process.env.LINEAGE_SCREENSHOT)await page.locator('#lineage-view').screenshot({path:process.env.LINEAGE_SCREENSHOT});
 const prior=posts.length;await page.locator('[data-lineage-root]').fill('../outside');await page.locator('#lineage-load').click();await expect(page.locator('[data-lineage-status]')).toContainText('有效批次编号');expect(posts.length).toBe(prior);expect(posts.every(path=>path==='/v1/session')).toBe(true);expect(errors).toEqual([]);
 console.log('PASS: tenant-local ancestry/impact, co-input exclusion, independent read root, partial depth, no history leakage/business writes, 390px and invalid root guard');
 }finally{await browser.close();}})().catch(error=>{console.error(error);process.exitCode=1});
