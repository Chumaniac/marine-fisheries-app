import {sourceExport,sourceCsv,sourceScopeMatches} from './result-export.js';
import {createOperationsView,validOperationsSummary,validOutboxPage,validLedgerStatus} from './operations-view.js';
import {createLineageView} from './lineage.js';
import {createCollectionInputGuard,inputMetadataKey} from './collection-input.js';
import {classificationOwner,classificationRegistration} from './classification-context.js';
import {collectionJourney} from './collection-journey.js';
import {serverCollection,deliverKnownOnce} from './server-collection.js';
import {COLLECTION_LIMITS,ownedCollection,sourceReference,createCollection,attachCollectionSources,guardCollection,collectionResult,collectionTimeline} from './collection.js';
import {projectBatches,prepareTransformParents,transformParentProblem,rebaseTransition,rebaseTransfer,transitions,stateNames,orgNames,describeResult,observationFreshness,batchDisposition} from './workflows.js';
import {deliverPending,usableSnapshot,scopedItems,validQueueActor,responseErrorMessage} from './network.js';
import {queueOverview,queueDiagnostics,MAX_DIAGNOSTIC_INPUT,operationLabel} from './queue-view.js';
import {createQualityCheck,qualityIssues} from './data-quality.js';
import {createAcceptanceReview,acceptanceIssues,evidencePolicies} from './collection-acceptance.js';
import {createWorkspaces} from './workspaces.js';
import {createReceiptReuse} from './receipt-reuse.js';
import {saveSnapshot} from './snapshots.js';
import {filterSourceEvents} from './source-filter.js';
import * as echarts from 'echarts/core';
import { LineChart, HeatmapChart, ScatterChart } from 'echarts/charts';
import { GridComponent, TooltipComponent, AriaComponent, VisualMapComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
echarts.use([LineChart,HeatmapChart,ScatterChart, GridComponent, TooltipComponent, AriaComponent, VisualMapComponent, CanvasRenderer]);
const $=id=>document.getElementById(id);
const themeStyle=getComputedStyle(document.documentElement);
const chartInk=themeStyle.getPropertyValue('--chart-ink').trim();
const chartText=themeStyle.getPropertyValue('--muted-foreground').trim();
const chartLine=themeStyle.getPropertyValue('--border').trim();
const observationAgeFormat=new Intl.RelativeTimeFormat('zh-CN',{numeric:'always'});
echarts.registerTheme('marine-light',{color:[chartInk],backgroundColor:'transparent',textStyle:{fontFamily:themeStyle.fontFamily,color:themeStyle.getPropertyValue('--muted-foreground').trim()},animation:false,tooltip:{backgroundColor:themeStyle.getPropertyValue('--card').trim(),borderColor:chartLine,textStyle:{color:themeStyle.getPropertyValue('--foreground').trim()}},categoryAxis:{axisLine:{lineStyle:{color:chartLine}},axisLabel:{color:chartText},nameTextStyle:{color:chartText},axisTick:{show:false}},valueAxis:{splitLine:{lineStyle:{color:chartLine}},axisLabel:{color:chartText},nameTextStyle:{color:chartText},axisLine:{show:false}}});
let chart=null,heatmap=null,geography=null,lastOverview=null;
let lineageView=null;
let operationsView=null;
function ensureOperationsView(){return operationsView??=createOperationsView({root:$('operations-workload'),getScope:()=>actor});}
function ensureLineageView(){return lineageView??=createLineageView({root:$('lineage-view'),request:api,getScope:()=>actor?.scopeHash||'',selectedBatch:()=>$('batch-list').value,stateNames,orgNames});}
function ensureOverviewCharts(){if(!chart){chart=echarts.init($('chart'),'marine-light');heatmap=echarts.init($('heatmap'),'marine-light');geography=echarts.init($('geography'),'marine-light');}}
let dataRequest=0,classificationRequest=0,traceRequest=0,outboxCursor=0,sourceCursor=0,traceContext=null,traceCursors={};
let batchAfter='',batchBefore='',batchPageBusy=false,batchPageCached=false,dashboardRequest=0;
let sourceExportBinding=null;
let dataEntryLoaded=false,dataEntryRefreshFailed=false,registeredSites=[],registeredDevices=[],collectionState={schema:1,items:[],activeId:null},collectionWriting=0;
let collectionEpoch=0,collectionPage=[],collectionCursor='',collectionHasMore=false,legacyCollectionState=null,collectionMembersAvailable=false;
let acceptanceView=null;
let reuseView={state:'idle',message:'',available:false,source:null};
let actor=null, visibleBatches=[], serverDashboard=null, pendingProjection=[], evidenceSelection=null, lastClassification=null, sourceEvents=[], transferRecords=[], currentTrace=null;
let lastQueueOrder=0;const operationReplies=new Map();const resolutionRequests=new Map();
let database, currentEvidence, timer, disposed=false, mode='normal', failures=0, rtt=0, syncing=false;
const collectionInputs=createCollectionInputGuard();let inputReviewBusy=false,inputReviewEpoch=0,lastInputFilters={};
const protectedInputForms=['collection-form','environment-form','catch-form','batch-form'];
const workspaces=createWorkspaces({document,window,onChange:name=>{if(name==='overview'&&lastOverview)paintOverview(lastOverview);}});
const databaseReady=new Promise((resolve,reject)=>{const open=indexedDB.open('marine-local-v1',2);open.onupgradeneeded=()=>{for(const name of ['snapshots','identity'])if(!open.result.objectStoreNames.contains(name))open.result.createObjectStore(name);if(!open.result.objectStoreNames.contains('queue'))open.result.createObjectStore('queue',{keyPath:'requestId'});};open.onsuccess=()=>{database=open.result;resolve();};open.onerror=()=>reject(open.error);});
async function transact(store,operation,access='readwrite'){await databaseReady;return new Promise((resolve,reject)=>{const tx=database.transaction(store,access);const request=operation(tx.objectStore(store));tx.oncomplete=()=>resolve(request?.result);tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error);});}
const snapshotKey=()=>`${actor?.scopeHash||"no-session"}:${$('filter-site').value}:${$('filter-species').value}${batchAfter||batchBefore?':'+JSON.stringify([batchAfter,batchBefore]):''}`;
const status=text=>{$('status').textContent=text;};
const qualityCheck=createQualityCheck({fetchReport:(scope,signal)=>api('/v1/data-quality?'+new URLSearchParams({siteId:scope.siteId,speciesId:scope.speciesId}),{signal,cache:'no-store'}),onState:renderQuality});
const acceptanceReview=createAcceptanceReview({getScope:()=>{const order=currentCollection();return {scopeHash:actor?.scopeHash,orderId:order?.id,orderRevision:order?.revision,online:mode!=='offline',subject:actor?.subject,role:actor?.role};},request:(path,options)=>api(path,{...options,cache:'no-store'}),onState:renderAcceptance});
function reuseScope(){const order=currentCollection();return {scopeHash:validQueueActor(actor)?actor.scopeHash:null,tenant:actor?.tenant,subject:actor?.subject,organization:actor?.organization,role:actor?.role,targetId:order?.id,siteId:order?.siteId,deviceId:order?.deviceId,startAt:order?.startAt,endAt:order?.endAt,revision:order?.revision,editable:Boolean(order?.serverPersisted&&!order.readOnly&&order.state==='open'),online:mode!=='offline',records:order?.records||[]};}
const receiptReuse=createReceiptReuse({getScope:reuseScope,readSource:(id,signal)=>api('/v1/collection-orders/'+encodeURIComponent(id)+'/record-receipts',{signal,cache:'no-store'}),sendLink:(id,body)=>api('/v1/collection-orders/'+encodeURIComponent(id)+'/records/reuse',{method:'POST',body:JSON.stringify(body),cache:'no-store'}),onState:renderReceiptReuse});

function renderAcceptance(value){
 acceptanceView={...value,scopeHash:actor?.scopeHash};renderCollectionJourney();
 const order=currentCollection(),selected=value.selected,report=selected?.assessment||value.preview,busy=value.state==='busy',online=validQueueActor(actor)&&mode!=='offline'&&Boolean(order);
 const owner=online&&actor.role==='operator'&&order.createdBy===actor.subject&&order.organization===actor.organization;
 for(const id of ['acceptance-preview','acceptance-history','acceptance-policy'])$(id).disabled=busy||!online;
 $('acceptance-submit').disabled=busy||!owner;$('acceptance-reason').disabled=busy;
 $('acceptance-summary').replaceChildren();$('acceptance-findings').replaceChildren();$('acceptance-list').replaceChildren();
 $('acceptance-state').textContent=busy?'正在核对工单，请稍候…':value.state==='error'?value.message:selected?(selected.current.stale?'这份历史验收对应的来源或状态已变化，请重新检查。':selected.decision?selected.decision.decision==='approve'?'独立验收已通过。':'独立审核已退回核对。':'检查已保存，等待独立审核。'):report?'检查完成，尚未作为验收记录保存。':'尚未检查；结束采集与验收通过分别记录。';
 if(report){
  const p=document.createElement('p');p.textContent=`${report.outcome==='ready'?'已声明检查通过':'仍需处理'} · 工单版本 ${report.orderRevision} · ${evidencePolicies[report.evidencePolicy]} · ${report.coverage.sources} 条来源 / ${report.coverage.records} 条回执。${report.coverage.complete?'':'当前检查未覆盖完整范围。'}`;$('acceptance-summary').append(p);
  for(const finding of report.findings){const li=document.createElement('li');li.textContent=`${acceptanceIssues[finding.code]} · ${finding.reference}`;$('acceptance-findings').append(li);}
  if(report.findingsTruncated){const li=document.createElement('li');li.textContent='仅列出前32处定位；全部问题计数保留在验收记录中。';$('acceptance-findings').append(li);}
 }
 if(selected?.decision){const p=document.createElement('p');p.textContent=`审核说明：${selected.decision.reason} · ${selected.decision.reviewedBy}`;$('acceptance-summary').append(p);}
 for(const item of value.items){const button=document.createElement('button');button.type='button';button.textContent=`版本 ${item.orderRevision} · ${evidencePolicies[item.evidencePolicy]} · ${item.decision==='approve'?'已通过':item.decision==='reject'?'已退回':item.outcome==='ready'?'待独立审核':'待处理'}`;button.disabled=busy;button.dataset.acceptanceId=item.id;button.addEventListener('click',()=>acceptanceReview.open(item.id));$('acceptance-list').append(button);}
 const reviewer=online&&actor.role==='auditor'&&selected&&actor.subject!==order.createdBy&&actor.subject!==selected.assessment.createdBy;
 $('acceptance-decision').hidden=!reviewer||Boolean(selected?.decision);$('acceptance-approve').disabled=busy||!reviewer||selected?.current.stale||selected?.current.outcome!=='ready';$('acceptance-reject').disabled=busy||!reviewer||selected?.current.stale;
 $('acceptance-certificate').hidden=selected?.decision?.decision!=='approve';$('acceptance-certificate').disabled=busy||!online;$('acceptance-certificate').textContent=selected?.current.stale?'导出历史验收凭证（当前已变化）':'导出验收凭证';
 if(!selected)$('acceptance-reason').value='';
}
function renderQuality(value){
 const busy=value.state==='busy',button=$('quality-check');button.disabled=busy;if(busy)button.setAttribute('aria-busy','true');else button.removeAttribute('aria-busy');
 const scope=value.scope||{siteId:$('filter-site').value,speciesId:$('filter-species').value};
 $('quality-scope').textContent=`${value.scope?'本次检查':'待检查'}范围：站点 ${scope.siteId||'未选择'} · 物种 ${scope.speciesId||'未选择'}。`;
 $('quality-counts').textContent='';$('quality-issues').replaceChildren();$('quality-findings').replaceChildren();
 if(value.state!=='ready'){$('quality-state').textContent=value.state==='busy'?'正在核对当前站点，请稍候。':value.state==='error'?value.message:'尚未检查。选择站点和物种后，检查已保存的来源数据。';return;}
 const r=value.report,labels={empty:'本次范围没有记录',checked:'本次范围未发现所列问题','needs-review':'有记录需要核对',partial:'已达到检查上限，结果为部分范围'};
 $('quality-state').textContent=labels[r.state]+` · ${new Date().toLocaleTimeString()} 检查时结果。`;
 $('quality-counts').textContent=`已检查 ${r.checkedRecords} 条、${r.checkedBytes} 字节；当前物种 ${r.counts.fishSelected} 条，其他物种 ${r.counts.fishOtherSpecies} 条，环境 ${r.counts.environment} 条，其他事件 ${r.counts.other} 条。${r.partial?'较早记录尚未检查，请缩小数据范围并人工核对。':''}`;
 for(const [code,label] of Object.entries(qualityIssues)){const count=r.issues[code];if(!count)continue;const item=document.createElement('li'),link=document.createElement('a');link.href=code.startsWith('sensor')||code==='environmentIncomplete'?'#environment-form':'#data-entry-title';link.textContent=`${label}：${count} 条；查看来源与录入区域`;item.append(link);$('quality-issues').append(item);}
 for(const finding of r.findings){const item=document.createElement('li');item.textContent=`本次第 ${finding.position+1} 条 · ${finding.eventId||'记录编号不符合格式'}：${finding.issues.map(code=>qualityIssues[code]).join('；')}`;$('quality-findings').append(item);}
 if(r.findingsTruncated){const item=document.createElement('li');item.textContent=`仅列出前32条定位，共 ${r.findingRecords} 条记录需要核对。`;$('quality-findings').append(item);}
}
$('quality-check').addEventListener('click',()=>qualityCheck.run({scopeHash:validQueueActor(actor)?actor.scopeHash:null,siteId:$('filter-site').value,speciesId:$('filter-species').value}));
for(const id of ['filter-site','filter-species'])$(id).addEventListener('input',()=>qualityCheck.reset());
function renderBatchDisposition(){
 const batchId=$('batch-list').value,batch=visibleBatches.find(item=>item.batchId===batchId),root=$('batch-disposition');
 const fresh=Boolean(traceContext&&traceContext===snapshotKey()&&currentTrace?.batch?.batchId===batchId&&batch?.revision===currentTrace.batch.revision&&mode!=='offline');
 const summary=batchDisposition({trace:currentTrace,queue:pendingProjection,actor,batchId,fresh});
 root.hidden=!validQueueActor(actor);const signature=JSON.stringify(summary);if(root.dataset.disposition===signature)return;root.dataset.disposition=signature;const label=$('batch-disposition-status');if(label.textContent!==summary.label)label.textContent=summary.label;
 const list=$('batch-disposition-items');list.replaceChildren(...summary.items.map(item=>{const row=document.createElement('li'),link=document.createElement('a'),detail=document.createElement('p');link.href='#'+item.target;link.textContent=item.title;detail.textContent=item.detail;row.append(link,detail);return row;}));
}

function clearVisible(resetForms=false){operationsView?.clear();lineageView?.clear(true);inputReviewEpoch++;inputReviewBusy=false;lockCollectionInputs(false);collectionInputs.clear();qualityCheck.reset();dataRequest++;classificationRequest++;collectionEpoch++;collectionPage=[];legacyCollectionState=null;collectionMembersAvailable=false;collectionState={schema:1,items:[],activeId:null};registeredDevices=[];registeredSites=[];renderLegacyCollections();$('collection-members').replaceChildren();renderCollection();batchAfter='';batchBefore='';batchPageBusy=false;batchPageCached=false;dashboardRequest++;traceContext=null;traceCursors={};traceRequest++;outboxCursor=0;$('outbox-list').replaceChildren();updateOutboxActions();for(const feedback of document.querySelectorAll('.form-feedback')){feedback.hidden=true;feedback.textContent='';}if(resetForms)for(const id of ['collection-form','batch-form','review-form','classify-form','evidence-form','transition-form','transfer-form','accept-form','transform-form','cold-form','site-form','device-form','environment-form','catch-form','data-import-form','resolve-cold-form'])$(id).reset();lastOverview=null;renderOverviewScope(null);chart?.clear();heatmap?.clear();geography?.clear();renderOverviewEmpty();for(const id of ['rows','heat-rows','geo-rows'])$(id).replaceChildren();$('count').textContent='—';$('prediction').textContent='未连接';$('age').textContent='—';$('pending').textContent='需登录';$('batch-list').replaceChildren();$('transform-parents').replaceChildren();$('classification').textContent='选择单鱼图片后开始识别';$('trace-result').textContent='选择批次后查询记录';$('review-list').replaceChildren();$('review-image').hidden=true;$('review-image').removeAttribute('src');currentEvidence=undefined;evidenceSelection=null;lastClassification=null;currentTrace=null;serverDashboard=null;pendingProjection=[];$('queue-filter').value='';renderQueue();visibleBatches=[];sourceEvents=[];sourceExportBinding=null;transferRecords=[];$('evidence-list').replaceChildren();$('queue-list').replaceChildren();$('monitor-result').textContent='尚未查询';$('incident-list').replaceChildren();$('ledger-job-list').replaceChildren();$('incident-note').value='';$('operations-state').textContent='尚未检查';for(const id of ['device-site','reading-device','catch-sources','cold-sources','cold-forecast','cold-issue-list','transfer-list','source-image-list'])$(id).replaceChildren();$('forecast-detail').textContent='尚未查询';$('transfer-state').textContent='暂无交接信息';$('data-result').textContent='尚未录入';dataEntryLoaded=false;dataEntryRefreshFailed=false;for(const id of ['device-site','reading-device'])delete $(id).dataset.requestedSelection;renderDataEntryGuidance();resetCatchSourceFilters();renderSourceImageDownload();renderBatchPage();}
async function dropActor(){const previousScope=actor?.scopeHash;actor=null;clearTimeout(timer);clearVisible(true);await transact('identity',s=>s.delete('current'));const keys=await transact('snapshots',s=>s.getAllKeys());for(const key of keys.filter(k=>k.startsWith(previousScope+':')))await transact('snapshots',s=>s.delete(key));}
async function acceptActor(value){if(!validQueueActor(value))throw new Error('无效会话身份');if(actor&&actor.scopeHash!==value.scopeHash){clearVisible(true);$('filter-site').value=value.mode==='local'?'synthetic-area-A':'';$('filter-species').value='saithe';}actor=value;await loadCollection();$('organization').value=value.organization||'Org1MSP';$('organization').disabled=value.mode!=='local';$('organization').closest('label').hidden=value.mode!=='local';$('connect').textContent=value.mode==='local'?'连接本地会话':'机构账号登录';evidenceSelection=await transact('identity',s=>s.get('evidence:'+actor.scopeHash));if(typeof evidenceSelection!=='object')evidenceSelection=null;currentEvidence=undefined;await transact('identity',s=>s.put(value,'current'));}
async function api(path,options={}){
 const {scopeHash:requestedScope,timeoutMs=8000,signal,...fetchOptions}=options;
 const identityRoute=path.startsWith('/v1/session')||path.startsWith('/v1/auth/')||path==='/v1/me';
 const boundScope=identityRoute?null:(requestedScope||actor?.scopeHash);
 const started=performance.now();const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),Math.min(45000,timeoutMs));
 const cancel=()=>controller.abort();if(signal?.aborted)cancel();else signal?.addEventListener('abort',cancel,{once:true});
 try{const response=await fetch(path,{...fetchOptions,credentials:'same-origin',signal:controller.signal,headers:{...(options.body instanceof FormData?{}:{'Content-Type':'application/json'}),...(boundScope?{'X-Marine-Scope':boundScope}:{}),...options.headers}});let body;try{body=await response.json();}catch{body={code:'INVALID_RESPONSE'};}if(boundScope&&actor?.scopeHash!==boundScope){const error=new Error('会话已切换，忽略旧响应');error.status=409;throw error;}if(!response.ok){const error=new Error(responseErrorMessage(response.status,body));error.status=response.status;error.code=body?.code;error.retryAfter=Number(response.headers.get('Retry-After'))||0;if((error.status===401||(error.status===403&&body?.detail==='Session scope changed'))&&!identityRoute)await dropActor();throw error;}failures=0;rtt=.7*rtt+.3*(performance.now()-started);mode=rtt>1500?'weak':'normal';return body;}catch(error){if(signal?.aborted)throw error;if(!error.status||[408,429,502,503,504].includes(error.status))failures++;if(failures>=3)mode='offline';throw error;}finally{clearTimeout(timeout);signal?.removeEventListener('abort',cancel);}
}
function draw(data,cached=false){
 serverDashboard=data;batchPageCached=cached;
 visibleBatches=projectBatches(data.batches,pendingProjection.filter(item=>!item.deliveryResult),actor);
 data={...data,batches:visibleBatches};renderBatchPage();
 lastOverview=data;if(workspaces.current==='overview')paintOverview(data);
 const selected=$('batch-list').value||evidenceSelection?.batchId;$('batch-list').replaceChildren(...data.batches.map(b=>{const option=document.createElement('option');option.value=b.batchId;option.textContent=`${b.batchId} · ${stateNames[b.state]||b.state}${b.openColdIssues?' · 暂停送达':''}${b.localConflict?'（需处理冲突）':b.localPending?'（待同步）':''}`;return option;}));if(data.batches.some(b=>b.batchId===selected))$('batch-list').value=selected;else if(selected)resetBatchTrace();currentEvidence=evidenceSelection?.batchId===$('batch-list').value?evidenceSelection.evidenceId:undefined;renderTransformParents();if(cached)traceContext=null;renderBatchDisposition();
 status(cached?'缓存快照 · 请刷新核对服务状态和预测有效期。':`${mode==='weak'?'弱网':'已连接'} · ${observationFreshness(data)} · 服务端待投递 ${data.pendingEvents}${pendingProjection.some(i=>i.blocked)?' · 有操作需处理，请展开待同步操作':''}`);
}
function renderOverviewScope(data){
 const same=data&&data.siteId===$('filter-site').value&&data.speciesId===$('filter-species').value;
 const label=data?`图表读取范围：${data.siteId||'站点未记录'} · ${data.speciesId||'物种未记录'}${same?'':'；当前筛选输入已变化，刷新后才会更新。'}`:'连接后显示图表的实际读取范围。';
 if($('overview-scope').textContent!==label)$('overview-scope').textContent=label;
}
for(const id of ['filter-site','filter-species'])$(id).addEventListener('input',()=>renderOverviewScope(lastOverview));
function renderOverviewEmpty(data=null,unavailable=false){
 const set=(id,value)=>{if($(id).textContent!==value)$(id).textContent=value;};
 $('chart-empty').hidden=Boolean(data?.series?.length);
 set('chart-sample-count',data?`${data.series.length} 个时间点`:'未读取数据');
 set('chart-empty-title',unavailable?'当前观测暂不可用':data?'当前范围暂无观测':'连接后查看实际观测');
 set('chart-empty-detail',unavailable?'恢复连接后刷新当前范围。原工单与待同步记录保持。':data?'可调整站点和物种后刷新，或在采集页登记来源。':'连接本地会话后，读取当前站点和物种的数据。');
 $('heatmap-empty').hidden=Boolean(data?.heatmap?.length);set('heatmap-empty',unavailable?'当前分布暂不可用':data?'当前范围暂无热力分布':'连接后查看海区分布');
 $('geography-empty').hidden=Boolean(data?.siteLocations?.length);set('geography-empty',unavailable?'当前地理信息暂不可用':data?'当前范围未登记站点坐标':'等待当前范围的站点信息');
}
// Charts and accessible statistic tables share the latest bounded dashboard.
function paintOverview(data){
 if(disposed||workspaces.current!=='overview')return;renderOverviewScope(data);
 if(mode!=='normal')data={...data,series:data.series.slice(-120),heatmap:(data.heatmap||[]).slice(-120)};
 renderOverviewEmpty(data);ensureOverviewCharts();chart.resize();heatmap.resize();geography.resize();
 $('count').textContent=String(data.observationCount);
 const ageSeconds=Number.isFinite(data.lastEventAt)&&data.lastEventAt>0?Math.max(0,Math.round(Date.now()/1000-data.lastEventAt)):null;
 const [ageUnit,ageDivisor]=ageSeconds>=86400?['day',86400]:ageSeconds>=3600?['hour',3600]:ageSeconds>=60?['minute',60]:['second',1];
 $('age').textContent=ageSeconds===null?'无数据':observationAgeFormat.format(-Math.floor(ageSeconds/ageDivisor),ageUnit);
 const p=data.prediction;const expired=p&&p.validUntil<Date.now()/1000;
 $('prediction').textContent=p?`${p.point.toFixed(2)}${expired?'（已过期）':''}`:'暂无预测';
 $('forecast-note').textContent=p?`${p.dataSource==='synthetic'?'合成数据模型':'现场数据模型（来源待审）'} · 区间 ${p.lower.toFixed(2)}–${p.upper.toFixed(2)} kg/标准作业小时 · ${expired?'已失效，不可用于新决策':'有效期5分钟'}；95%为名义覆盖。`:'暂无可用预测，请查看观测记录和预测状态。';
 chart.setOption({aria:{enabled:true},grid:{left:40,right:15,bottom:30,top:20},tooltip:{trigger:'axis'},xAxis:{type:'category',data:data.series.map(r=>r.minute.slice(11))},yAxis:{type:'value',minInterval:1},series:[{type:'line',data:data.series.map(r=>r.count),showSymbol:data.series.length===1,symbolSize:7,sampling:'lttb',lineStyle:{color:chartInk,width:2},areaStyle:{color:chartInk,opacity:.08}}]},{notMerge:true});
 $('rows').replaceChildren(...data.series.map(r=>{const row=document.createElement('tr');for(const value of [r.minute,r.count]){const cell=document.createElement('td');cell.textContent=value;row.append(cell);}return row;}));
 const heat=data.heatmap||[],sites=[...new Set(heat.map(r=>r.siteId))].slice(0,20),minutes=[...new Set(heat.map(r=>r.minute))].sort().slice(-24);
 const heatPoints=heat.filter(r=>sites.includes(r.siteId)&&minutes.includes(r.minute)).map(r=>[minutes.indexOf(r.minute),sites.indexOf(r.siteId),r.count]);
 heatmap.setOption({aria:{enabled:true},tooltip:{},grid:{left:110,right:15,bottom:65,top:10},xAxis:{type:'category',data:minutes.map(m=>m.slice(11))},yAxis:{type:'category',data:sites,axisLabel:{width:90,overflow:'truncate'}},visualMap:{min:0,max:Math.max(1,...heat.map(r=>r.count)),orient:'horizontal',left:'center',bottom:0,textStyle:{color:chartText},inRange:{color:['#152b3a','#245365','#347b80']}},series:[{type:'heatmap',data:heatPoints,label:{show:true,color:'#f0f7f7',formatter:({value})=>String(value[2])}}]},{notMerge:true});
 const points=data.siteLocations||[];$('geo-note').textContent=points.length?`经纬度按0.1°聚合；${points.some(p=>p.dataSource==='synthetic')?'含合成示例坐标，非真实作业位置。':'仅展示登记的授权站点。'}`:'登记站点坐标后显示地图。';
 geography.setOption({aria:{enabled:true},tooltip:{},grid:{left:45,right:20,bottom:40,top:20},xAxis:{name:'经度',type:'value',min:-180,max:180},yAxis:{name:'纬度',type:'value',min:-90,max:90},series:[{type:'scatter',data:points.map(p=>({name:p.siteId,value:[p.longitude,p.latitude,p.count]})),symbolSize:12,itemStyle:{color:chartInk}}]},{notMerge:true});
 for(const [id,values] of [['heat-rows',heat.filter(r=>sites.includes(r.siteId)&&minutes.includes(r.minute)).map(r=>[r.minute,r.siteId,r.count])],['geo-rows',points.map(p=>[p.siteId,p.longitude,p.latitude,p.count])]])$(id).replaceChildren(...values.map(values=>{const row=document.createElement('tr');for(const value of values){const cell=document.createElement('td');cell.textContent=String(value);row.append(cell);}return row;}));
}
// An unavailable observation snapshot does not invalidate separately persisted
// workorders or outbox entries. Account changes still use clearVisible().
function showMissingDashboard(){
 serverDashboard=null;visibleBatches=[];lastOverview=null;batchPageCached=true;
 renderOverviewScope(null);renderOverviewEmpty(null,true);chart?.clear();heatmap?.clear();geography?.clear();
 for(const id of ['rows','heat-rows','geo-rows'])$(id).replaceChildren();
 $('count').textContent='—';$('prediction').textContent='无有效缓存';$('age').textContent='—';
 $('geo-note').textContent='当前范围没有有效观测缓存。';$('batch-list').replaceChildren();resetBatchTrace();
 $('pending').textContent=String(scopedItems(pendingProjection,actor).length);
 renderBatchPage();renderQueue();renderCollection();
}
async function refresh({preserveOnError=false}={}){
 if(!validQueueActor(actor)){clearVisible();status('请连接有效会话。');return false;}
 const scope=actor.scopeHash,key=snapshotKey(),request=++dashboardRequest;
 try{const data=await api('/v1/dashboard?'+new URLSearchParams({siteId:$('filter-site').value,speciesId:$('filter-species').value,batchAfter,batchBefore}));if(request!==dashboardRequest||actor?.scopeHash!==scope||snapshotKey()!==key)return false;if(data.tenant!==actor.tenant)throw new Error('服务端租户不匹配');draw(data);await databaseReady;await saveSnapshot(database,key,data);return true;}
 catch(error){if(request!==dashboardRequest||actor?.scopeHash!==scope||snapshotKey()!==key)return false;traceContext=null;renderBatchDisposition();if([401,403].includes(error.status)){await dropActor();status('会话已失效，请重新连接。');return false;}if(preserveOnError){status('批次列表未更新，当前页和选择已保留，请恢复连接后重试。');return false;}const snapshot=await transact('snapshots',s=>s.get(key));if(usableSnapshot(snapshot))draw(snapshot.data,true);else{showMissingDashboard();status(`当前观测无有效缓存，工单与待同步记录仍保留：${error.message}`);}return false;}
}

function validBatchCursor(value){return typeof value==='string'&&value.length>0&&value.length<=128?value:null;}
function renderBatchPage(){
 const unavailable=batchPageBusy||batchPageCached||mode==='offline'||!validQueueActor(actor);
 for(const [id,disabled] of [['batch-first',unavailable||!(batchAfter||batchBefore)],['batch-prev',unavailable||!validBatchCursor(serverDashboard?.previousBatchCursor)],['batch-next',unavailable||!validBatchCursor(serverDashboard?.nextBatchCursor)]]){const button=$(id);button.dataset.stateDisabled=String(disabled);button.disabled=disabled;}
 $('batch-page').textContent=serverDashboard?`本页 ${serverDashboard.batches.length} 个批次${serverDashboard.hasMoreBatches?'，后面还有批次':''}。${batchPageCached?'离线快照，连接恢复后可翻页。':'选择本页批次后，请重新查询溯源。'}`:'连接后读取批次列表。';
}
async function pageBatches(direction){
 if(batchPageBusy||batchPageCached||mode==='offline')throw Error('请恢复连接并刷新后再翻页。');
 const next=direction==='next'?validBatchCursor(serverDashboard?.nextBatchCursor):direction==='previous'?validBatchCursor(serverDashboard?.previousBatchCursor):'';
 if(next===null)return;
 const scope=actor?.scopeHash,previous=[batchAfter,batchBefore];batchPageBusy=true;
 batchAfter=direction==='next'?next:'';batchBefore=direction==='previous'?next:'';renderBatchPage();
 try{if(!await refresh({preserveOnError:true})&&actor?.scopeHash===scope)[batchAfter,batchBefore]=previous;}
 finally{batchPageBusy=false;renderBatchPage();}
}
action('batch-first',()=>pageBatches('first'));
action('batch-prev',()=>pageBatches('previous'));
action('batch-next',()=>pageBatches('next'));

function renderQueue(){
 const overview=queueOverview(pendingProjection,actor),previous=$('queue-list').value,filter=$('queue-filter').value;
 const labels={manual:'需处理',waiting:'等待同步',ready:'准备同步'};
 $('queue-summary').textContent=`当前账户共 ${overview.counts.all} 条 · 需处理 ${overview.counts.manual} · 等待 ${overview.counts.waiting} · 准备 ${overview.counts.ready}`;
 const visible=overview.rows.filter(row=>!filter||row.state===filter);
 $('queue-list').replaceChildren(...visible.map(row=>{const option=document.createElement('option');option.value=row.requestId;option.textContent=`${labels[row.state]} · ${row.label} · 已尝试 ${row.attemptsKnown?row.attempts:'未知'}/${row.maximumAttempts}`;return option;}));
 if(visible.some(row=>row.requestId===previous))$('queue-list').value=previous;
 const selected=visible.find(row=>row.requestId===$('queue-list').value);
 $('queue-detail').textContent=selected?`${selected.label} · ${labels[selected.state]}
已尝试 ${selected.attemptsKnown?selected.attempts:'未知'}/${selected.maximumAttempts} 次
${selected.hint}`:overview.counts.all?'当前分类没有记录，可选择“全部”。':'没有待同步操作。';
 for(const id of ['queue-retry','queue-rebase','queue-remove']){$(id).dataset.stateDisabled=String(!selected);$(id).disabled=!selected;}
 renderBatchDisposition();renderCollection();
 const exportButton=$('queue-diagnostics');exportButton.dataset.stateDisabled=String(!validQueueActor(actor));exportButton.disabled=!validQueueActor(actor)||exportButton.hasAttribute('aria-busy');
}
$('queue-filter').addEventListener('change',renderQueue);
$('queue-list').addEventListener('change',()=>{const overview=queueOverview(pendingProjection,actor),selected=overview.rows.find(row=>row.requestId===$('queue-list').value);$('queue-detail').textContent=selected?`${selected.label}
已尝试 ${selected.attemptsKnown?selected.attempts:'未知'}/${selected.maximumAttempts} 次
${selected.hint}`:'请选择一条记录。';});
async function syncQueue(){if(syncing||!validQueueActor(actor))return;syncing=true;const current=actor;try{const queue=scopedItems(await transact('queue',s=>s.getAll()),current);await deliverPending(queue,{send:async(body,item)=>{if(item.file&&(!(item.file instanceof Blob)||item.file.size>256*1024)){const error=new Error('文件队列大小不合法');error.status=413;throw error;}let payload=JSON.stringify(body);if(item.file){payload=new FormData();payload.append('file',item.file);}const sendBusiness=async()=>{if(item.collectionServer){const latest=serverCollection(await api('/v1/collection-orders/'+encodeURIComponent(item.collectionId)),current);guardCollection({...latest,records:latest.records.filter(row=>row.requestId!==item.requestId)},current,item.path,body,registeredDevices,sourceEvents);}return api(item.path||'/v1/batches',{method:item.method||'POST',body:payload,scopeHash:current.scopeHash});};const result=item.collectionId||item.path==='/v1/collection-orders'?await deliverKnownOnce(item,{send:sendBusiness,persist:queued=>transact('queue',s=>s.put(queued)),link:received=>receiveCollection(item,received)}):await sendBusiness();operationReplies.set(item.requestId,result);if(operationReplies.size>100)operationReplies.delete(operationReplies.keys().next().value);if(item.path==='/v1/collection-orders')status('工单已保存到服务器。');else $('trace-result').textContent=describeResult(result);if(result.evidenceId){const batchId=item.batchId||decodeURIComponent((item.path||'').match(/^\/v1\/batches\/([^/]+)/)?.[1]||'');evidenceSelection={batchId,evidenceId:result.evidenceId};currentEvidence=$('batch-list').value===batchId?result.evidenceId:undefined;await transact('identity',s=>s.put(evidenceSelection,'evidence:'+current.scopeHash));}},remove:id=>transact('queue',s=>s.delete(id)),save:item=>transact('queue',s=>s.put(item))});if(actor?.scopeHash!==current.scopeHash)return;const remaining=scopedItems(await transact('queue',s=>s.getAll()),actor);pendingProjection=remaining;$('pending').textContent=String(remaining.length);renderQueue();if(remaining.some(x=>x.blocked))status('当前账户存在需处理的同步冲突，记录已保留。');}finally{syncing=false;renderCollection();}}
async function cycle(){
 const identity=actor;if(disposed||!identity)return;
 try{
  if(!validQueueActor(identity)){await dropActor();status('会话已过期，请重新连接。');return;}
  await syncQueue();if(disposed||actor!==identity)return;
  await refresh();if(disposed||actor!==identity)return;
  if(!validQueueActor(identity)){await dropActor();status('会话已过期，请重新连接。');return;}
 }catch{if(!disposed&&actor===identity)status('本机同步未完成，请核对存储并重新连接。');return;}
 timer=setTimeout(cycle,mode==='normal'?15000:60000);
}
function action(id,fn){$(id).addEventListener('click',async()=>{const b=$(id);b.disabled=true;b.setAttribute('aria-busy','true');try{await fn();}catch(error){status(error.message);}finally{b.disabled=b.dataset.stateDisabled==='true';b.removeAttribute('aria-busy');}});}
action('connect',async()=>{const config=await api('/v1/auth/config');if(config.mode!=='local'){if(!config.browserLogin)throw new Error('机构登录尚未配置，请联系管理员');location.assign(config.loginPath);return;}await acceptActor(await api('/v1/session?organization='+encodeURIComponent($('organization').value),{method:'POST',headers:{'X-Requested-With':'marine-web'}}));await transact('identity',s=>s.delete('logoutPending'));const queued=await transact('queue',s=>s.getAll());for(const item of queued){if(!item.scopeHash&&actor.mode==='local'&&actor.tenant==='demo'&&actor.subject==='local-operator'){await transact('queue',s=>s.put({...item,tenant:actor.tenant,subject:actor.subject,scopeHash:actor.scopeHash,blocked:false,next:0}));}else if(item.scopeHash===actor.scopeHash&&item.lastStatus===401)await transact('queue',s=>s.put({...item,blocked:false,next:0,attempt:0,retryStartedAt:Date.now()}));}await syncQueue();await refresh();await loadData();await loadTransfers();clearTimeout(timer);timer=setTimeout(cycle,15000);});
action('refresh',async()=>{if(await refresh())await loadData();});
action('logout',async()=>{let confirmed=false;try{await api('/v1/session',{method:'DELETE'});confirmed=true;}finally{await dropActor();await transact('identity',s=>s.put(!confirmed,'logoutPending'));status(confirmed?'已退出。未确认操作仅在原账户重新登录后同步。':'已清除本地显示，联网后完成服务端退出。');}});
function submit(id,fn){const form=$(id),feedback=document.createElement('p');feedback.className='form-feedback';feedback.setAttribute('role','alert');feedback.hidden=true;form.append(feedback);form.addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter||form.querySelector('button:not([type=button])');button.disabled=true;button.setAttribute('aria-busy','true');feedback.hidden=true;try{const message=await fn();if(typeof message==='string'){feedback.classList.remove('is-error');feedback.setAttribute('role','status');feedback.textContent=message;feedback.hidden=false;}}catch(error){feedback.classList.add('is-error');feedback.setAttribute('role','alert');feedback.textContent=error.message;feedback.hidden=false;status(error.message);}finally{button.disabled=button.dataset.stateDisabled==='true';button.removeAttribute('aria-busy');}});}
function classificationContext(){
 const order=currentCollection(),device=registeredDevices.find(item=>item.deviceId===$('reading-device').value);
 return classificationOwner({scopeHash:validQueueActor(actor)?actor.scopeHash:null,orderId:order?.id,revision:order?.revision,siteId:device?.siteId||order?.siteId||$('filter-site').value,deviceId:$('reading-device').value||null});
}
function renderClassificationRegistration(){
 const capture=lastClassification,current=classificationContext(),view=classificationRegistration(capture,current);
 $('classification-owner').hidden=!capture;
 $('classification-origin').hidden=!capture?.owner?.orderId||capture.owner.orderId===current.orderId||capture.owner.scopeHash!==current.scopeHash;
 $('classification-reuse').hidden=!view.canReuse;
 $('classification-reuse').textContent='已核对，复用现有结果到 '+(current.orderId||'当前设备');
 const button=$('record-classification');button.dataset.stateDisabled=String(!view.canRecord);if(!button.hasAttribute('aria-busy'))button.disabled=!view.canRecord;
 button.textContent=capture?.event&&!capture.recorded?'读取原登记回执，未收件时按原请求重试':'登记本次分类观测';
 if(!capture){$('classification-owner').textContent='';return;}
 const origin=`结果归属：${capture.owner.orderId||'未选工单'} · ${capture.owner.siteId||'未选站点'} · ${capture.owner.deviceId||'未选设备'}`;
 $('classification-owner').textContent=origin+'。'+(view.state==='recorded'?(capture.registrationStatus==='review_rejected'?'原观测已被人工拒绝，图片与结果保留，不进入工单来源。':'已登记，图片与结果保留；工单来源仍需明确选择。'):view.state==='foreign'?'账户已变化，不能复用原账户结果。':view.state==='changed'?(capture.event?'原登记请求已固定，请返回原工单与设备读取回执；不会换绑或重新识别。':'当前工单、版本或设备已变化。核对图片、原归属和目标工单后，可明确复用现有结果；不会重新识别。'):view.state==='pending'?'上次登记结果待核对，先读取原回执；只允许重试同一个事件与原图。':'请核对图片、当前设备与工单时间范围后登记。');
}
action('classification-reuse',async()=>{
 const capture=lastClassification,context=classificationContext();if(!classificationRegistration(capture,context).canReuse)throw Error('结果归属已变化或原登记尚未确认，请先核对。');
 capture.owner=classificationOwner(context);renderClassificationRegistration();status('已明确选择复用现有图片与结果；尚未登记，不会自动重新识别。');
});
action('classification-origin',async()=>{const capture=lastClassification;if(capture?.owner?.scopeHash!==actor?.scopeHash||!capture?.owner?.orderId)return;await openServerCollection(capture.owner.orderId);});
submit('classify-form',async()=>{
 if(lastClassification?.event&&!lastClassification.recorded)throw Error('原登记尚未确认，请先读取回执，原图与结果仍保留。');
 const file=$('image').files[0];if(!file)throw new Error('请先选择单鱼图片');if(file.size>8*1024*1024)throw new Error('图片不能超过 8 MB，请缩小后再识别。');
 const request=++classificationRequest,owner=classificationContext(),observedAt=new Date().toISOString(),form=new FormData();form.append('image',file);lastClassification=null;renderClassificationRegistration();
 $('classification').textContent='正在本机运行模型…';
 try{
  const result=await api('/v1/classifications',{method:'POST',body:form});
  if(request!==classificationRequest||$('image').files[0]!==file)return;
  lastClassification={result,file,owner,observedAt,requestId:crypto.randomUUID(),recorded:false};renderClassificationRegistration();
  $('classification').textContent=`图片：${file.name||'已选图片'}\n识别结果：${result.speciesId==='saithe'?'绿青鳕':'鲑鳟类'}\n模型置信度：${(result.confidence*100).toFixed(1)}%${result.needsReview?'（建议人工复核）':''}\n复核队列：${result.reviewStatus==='heldout_excluded'?'留出评估图片，不进入训练':result.reviewId?'已保留待复核':'未加入，请查看说明'}\n模型：${result.modelVersion}；仅适用于当前两类公开数据范围。`;
 }catch(error){if(request!==classificationRequest)return;$('classification').textContent='尚未取得识别结果，原图仍保留。';throw error;}
});
$('image').addEventListener('change',()=>{classificationRequest++;if(lastClassification?.event&&!lastClassification.recorded){status('上次登记尚未确认，原图和结果仍保留；请先读取原登记回执，再识别新图。');return;}lastClassification=null;renderClassificationRegistration();$('classification').textContent='图片已选择，请开始识别。';});
async function enqueueOperation(path,body,file=null,batchId=null){
 const inputForm={'/v1/collection-orders':'collection-form','/v1/environment-readings':'environment-form','/v1/catches':'catch-form','/v1/batches':'batch-form'}[path],inputTicket=collectionInputs.ticket(inputForm);if(inputForm)assertCollectionInputs();
 collectionWriting++;try{const operation=currentCollection();const related=operation&&!file&&['/v1/environment-readings','/v1/catches','/v1/batches'].includes(path);if(related)guardCollection(operation,actor,path,body,registeredDevices,sourceEvents);
 if(!validQueueActor(actor))throw new Error('请先连接有效会话');
 const all=await transact('queue',s=>s.getAll()),queue=scopedItems(all,actor);
 if(related&&operation.records.length+queue.filter(item=>item.collectionId===operation.id).length>=COLLECTION_LIMITS.records)throw new Error('作业记录容量不足，原队列保留。');
 if(queue.length>=100||all.length>=1000)throw new Error('离线队列已满，请先同步或导出待处理记录。');
 if(all.reduce((sum,item)=>sum+(item.file?.size||0),0)+(file?.size||0)>32*1024*1024)throw new Error('本地文件队列达到32MiB上限。');
 const requestId=body?.requestId||(path==='/v1/collection-orders'?body.id:crypto.randomUUID());
 const dependencies=[...new Set([path==='/v1/collection-orders'?'collection:'+body.id:body?.batchId||$('batch-list').value||(related&&operation.serverPersisted?'collection:'+operation.id:path),...Object.keys(body?.parents||{}),...(body?.outputs||[]).map(o=>o.batchId),...(related?['collection:'+operation.id]:[])])];
 lastQueueOrder=Math.max(Date.now(),lastQueueOrder+1,...all.map(i=>(i.createdAt||0)+1));
 await transact('queue',s=>s.put({requestId,path,body,file,collectionId:related?operation.id:null,collectionServer:Boolean(related&&operation.serverPersisted),batchId:batchId||$('batch-list').value,dependencies,createdAt:lastQueueOrder,tenant:actor.tenant,subject:actor.subject,scopeHash:actor.scopeHash,attempt:0,next:0,blocked:false}));
 if(inputForm){collectionInputs.saved(inputForm,inputTicket);renderCollectionInputGuard();}
 await syncQueue();await refresh();const waiting=await transact('queue',store=>store.get(requestId));return operationReplies.get(requestId)||{queued:true,received:Boolean(waiting?.deliveryResult)};
 }finally{collectionWriting--;renderCollection();}
}
submit('batch-form',()=>enqueueOperation('/v1/batches',{requestId:crypto.randomUUID(),batchId:$('batch-id').value,speciesId:$('species').value,massGrams:Number($('mass').value),areaCode:$('area').value}));
async function traceSelected(id=$('batch-list').value,page='all'){
 if(!id)throw new Error('请先选择批次');
 const request=++traceRequest,previous=currentTrace,context=snapshotKey();traceContext=null;renderBatchDisposition();
 const pages={evidence:['evidenceAfter','nextEvidenceCursor','evidence','hasMoreEvidence'],ledger:['ledgerAfter','nextLedgerCursor','ledgerCommands','hasMoreLedgerCommands'],issues:['issueAfter','nextIssueCursor','coldIssues','hasMoreColdIssues']};
 const selected=pages[page];
 if(selected&&(!previous||previous.batch.batchId!==id||!previous[selected[3]]))throw new Error('请先查询批次并选择还有后续的记录');
 const cursors=page!=='all'&&previous?.batch?.batchId===id?{...traceCursors}:{};
 if(selected)cursors[selected[0]]=previous[selected[1]];
 const query=new URLSearchParams(cursors).toString();
 const result=await api(`/v1/batches/${encodeURIComponent(id)}/trace${query?'?'+query:''}`);
 if(request!==traceRequest||context!==snapshotKey())return;
 currentTrace=result;traceContext=context;traceCursors=cursors;
 for(const [kind,fields] of Object.entries(pages)){const button=$('trace-more-'+kind);button.dataset.stateDisabled=String(!result[fields[3]]);button.disabled=!result[fields[3]];}

 $('trace-result').textContent=describeResult(result);
 const position=visibleBatches.findIndex(batch=>batch.batchId===id);
 if(position<0){visibleBatches.push(result.batch);const option=document.createElement('option');option.value=id;option.textContent=`${id} · ${stateNames[result.batch.state]}`;$('batch-list').append(option);}
 else if(!visibleBatches[position].localPending&&!visibleBatches[position].localConflict){visibleBatches[position]=result.batch;const option=[...$('batch-list').options].find(row=>row.value===id);if(option)option.textContent=`${id} · ${stateNames[result.batch.state]}`;}
 $('batch-list').value=id;renderBatchDisposition();
 $('evidence-list').replaceChildren(...result.evidence.map(e=>{const option=document.createElement('option');option.value=e.evidenceId;option.textContent=`${e.evidenceId.slice(0,12)} · ${e.status==='COMMITTED'?'已存证':e.status==='PIN_VERIFIED'?'副本已核验':'待存证'}`;return option;}));
 if(evidenceSelection?.batchId===id&&result.evidence.some(e=>e.evidenceId===evidenceSelection.evidenceId))$('evidence-list').value=evidenceSelection.evidenceId;
 currentEvidence=$('evidence-list').value;evidenceSelection={batchId:id,evidenceId:currentEvidence};
 const savedSelection=evidenceSelection,scope=actor?.scopeHash;
 if(scope)await transact('identity',s=>s.put(savedSelection,'evidence:'+scope));
 if(request!==traceRequest||$('batch-list').value!==id)return;
 $('cold-issue-list').replaceChildren(...(result.coldIssues||[]).filter(i=>i.status==='OPEN').map(i=>{const o=document.createElement('option');o.value=i.issueId;o.textContent=`${i.record.minimumC}–${i.record.maximumC}°C，允许 ${i.record.allowedMinimumC}–${i.record.allowedMaximumC}°C · 暂停送达`;return o;}));
 const verification=await api(`/v1/batches/${encodeURIComponent(id)}/ledger-verify`).catch(error=>{if(request===traceRequest){traceContext=null;renderBatchDisposition();}throw error;});if(request!==traceRequest||$('batch-list').value!==id)return;currentTrace.ledgerVerification=verification.verified?'verified':'unconfirmed';renderBatchDisposition();$('ledger-result').textContent=verification.verified?'批次状态与链上记录一致':verification.status==='unavailable'?'链服务暂不可用，本地记录已保留':'链上状态待确认；本地保存不等于存证完成';
}
action('trace',traceSelected);
$('lineage-load')?.addEventListener('click',()=>void ensureLineageView().load());
$('lineage-clear')?.addEventListener('click',()=>ensureLineageView().clear(true));
$('batch-list').addEventListener('change',()=>lineageView?.clear());
for(const kind of ['evidence','ledger','issues'])action('trace-more-'+kind,()=>traceSelected($('batch-list').value,kind));
$('evidence-list').addEventListener('change',async()=>{currentEvidence=$('evidence-list').value;evidenceSelection={batchId:$('batch-list').value,evidenceId:currentEvidence};if(actor)await transact('identity',s=>s.put(evidenceSelection,'evidence:'+actor.scopeHash));});
function resetBatchTrace(){traceContext=null;traceCursors={};traceRequest++;currentEvidence=undefined;evidenceSelection=null;currentTrace=null;$('evidence-list').replaceChildren();$('cold-issue-list').replaceChildren();$('trace-result').textContent='批次已切换，请查询后选择对应证据';$('ledger-result').textContent='尚未核对此批次';renderBatchDisposition();}
$('batch-list').addEventListener('change',resetBatchTrace);
submit('evidence-form',async()=>{if(!$('batch-list').value)throw new Error('请先选择批次');const file=$('evidence-file').files[0];if(file.size>256*1024)throw new Error('证据文件超过256KiB');await enqueueOperation(`/v1/batches/${encodeURIComponent($('batch-list').value)}/evidence`,null,file);});
function selectedBatch(){const batch=visibleBatches.find(b=>b.batchId===$('batch-list').value);if(!batch)throw new Error('请先选择已有批次');if(batch.localConflict)throw new Error('此批次存在待处理冲突，请先核对队列');return batch;}
submit('transition-form',()=>{const batch=selectedBatch();const next=$('next-state').value;if(!transitions[batch.state]?.includes(next))throw new Error('当前状态不能执行这一步，请核对批次');if(next==='DELIVERED'&&batch.openColdIssues)throw new Error('存在冷链异常，请先查询溯源并记录处置');return enqueueOperation(`/v1/batches/${encodeURIComponent(batch.batchId)}/transitions`,{requestId:crypto.randomUUID(),expectedRevision:batch.revision,nextState:$('next-state').value});});
submit('transfer-form',async()=>{const batch=selectedBatch();await enqueueOperation(`/v1/batches/${encodeURIComponent(batch.batchId)}/transfers`,{requestId:crypto.randomUUID(),expectedRevision:batch.revision,target:$('transfer-org').value});await loadTransfers();});
submit('accept-form',async()=>{const proposal=transferRecords.find(t=>t.proposalId===$('proposal-id').value);if(!proposal)throw new Error('请先刷新并选择待接收交接');await enqueueOperation(`/v1/transfers/${encodeURIComponent(proposal.proposalId)}/accept`,{requestId:crypto.randomUUID(),expectedRevision:proposal.revision},null,proposal.batchId);await loadTransfers();});
submit('transform-form',()=>{const ids=[...$('transform-parents').selectedOptions].map(o=>o.value);const selected=ids.length?ids:[selectedBatch().batchId];const {parents,total}=prepareTransformParents(visibleBatches,selected,actor?.organization);const outputs=[...document.querySelectorAll('.output-row')].map(row=>{const fields=row.querySelectorAll('input');return {requestId:crypto.randomUUID(),batchId:fields[0].value,massGrams:Number(fields[1].value)};});const loss=Number($('loss-mass').value);if(outputs.reduce((sum,o)=>sum+o.massGrams,0)+loss!==total)throw new Error(`输入总重 ${total} 克，产出加损耗必须相等`);return enqueueOperation('/v1/batch-transforms',{requestId:crypto.randomUUID(),parents,outputs,lossGrams:loss});});
submit('cold-form',()=>{const batch=selectedBatch();if(!$('cold-manual').checked&&!$('cold-sources').selectedOptions.length&&!$('cold-forecast').value)throw new Error('请选择关联来源，或明确勾选手工记录');return enqueueOperation(`/v1/batches/${encodeURIComponent(batch.batchId)}/cold-chain`,{requestId:crypto.randomUUID(),startAt:Date.parse($('cold-start').value)/1000,endAt:Date.parse($('cold-end').value)/1000,minimumC:Number($('cold-min').value),maximumC:Number($('cold-max').value),allowedMinimumC:Number($('allowed-min').value),allowedMaximumC:Number($('allowed-max').value),deviceId:$('cold-device').value,sourceEventIds:$('cold-manual').checked?[]:[...$('cold-sources').selectedOptions].map(o=>o.value),forecastId:$('cold-manual').checked?null:($('cold-forecast').value||null)});});
action('sync-ledger',async()=>{$('trace-result').textContent=describeResult(await api('/v1/ledger/sync',{method:'POST',timeoutMs:45000}));});
action('queue-retry',async()=>{const item=await transact('queue',s=>s.get($('queue-list').value));if(!item||!scopedItems([item],actor).length)throw new Error('请选择当前账户记录');await transact('queue',s=>s.put({...item,blocked:false,next:0,attempt:0,retryStartedAt:Date.now()}));await syncQueue();});
action('queue-remove',async()=>{const item=await transact('queue',s=>s.get($('queue-list').value));if(!item||!scopedItems([item],actor).length)return;if(!confirm('删除这条尚未确认的本地操作？原文件不会从设备删除，已提交服务端的记录也不会撤销。'))return;await transact('queue',s=>s.delete(item.requestId));await syncQueue();});
action('monitor',async()=>{$('monitor-result').textContent=JSON.stringify(await api('/v1/monitoring'),null,2);});
let evidenceWorking=false;
async function operateEvidence(operation){
 if(evidenceWorking)throw new Error('已有证据操作正在处理，请等待返回结果。');
 const id=$('evidence-list').value,batchId=$('batch-list').value,context=snapshotKey();
 if(!id||id!==currentEvidence||!batchId)throw new Error('请查询当前批次并选择已有证据');
 evidenceWorking=true;for(const control of ['pin','anchor','verify','complete-evidence'])$(control).disabled=true;try{
 const current=()=>context===snapshotKey()&&$('batch-list').value===batchId&&currentEvidence===id;
 const evidence=currentTrace?.batch?.batchId===batchId?currentTrace.evidence?.find(row=>row.evidenceId===id):null;
 const known=evidence?.status;
 const steps=operation==='complete'?(known==='COMMITTED'?['verify']:['PIN_VERIFIED','SUBMITTED'].includes(known)?['anchor','verify']:['pin','anchor','verify']):[operation];
 let result,error,returned=0;
 traceContext=null;renderBatchDisposition();
 try{for(const step of steps){result=await api(`/v1/evidence/${encodeURIComponent(id)}/${step}`,{method:step==='verify'?'GET':'POST',timeoutMs:45000});if(!result||typeof result!=='object'||Array.isArray(result))throw new Error('服务返回结果不完整，请先查询证据状态。');returned++;
  const state=result?.chainStatus||result?.status;
  if(current()&&evidence&&['PIN_VERIFIED','SUBMITTED','COMMITTED'].includes(state))evidence.status=state;
 }}catch(reason){error=reason;}
 if(current()){
  let refreshed=true;try{await traceSelected(batchId,'current');}catch{refreshed=false;}
  if(current()){
   if(result)$('trace-result').textContent=describeResult(result);
   if(refreshed&&!error)status('证据处理结果已更新，请查看详情。');
   if(!refreshed){traceContext=null;renderBatchDisposition();status('操作响应已返回，列表刷新未完成。请重新查询批次，避免重复提交。');}
  }
 }
 if(error)throw new Error((returned?'已保留已返回的步骤结果；':'')+error.message);
 }finally{evidenceWorking=false;for(const control of ['pin','anchor','verify','complete-evidence'])$(control).disabled=$(control).dataset.stateDisabled==='true';}
}
for(const id of ['pin','anchor','verify'])action(id,()=>operateEvidence(id));
const resize=new ResizeObserver(()=>{if(workspaces.current==='overview'){chart?.resize();heatmap?.resize();geography?.resize();}});resize.observe($('chart'));
window.addEventListener('online',()=>{mode='normal';renderCollectionInputGuard();syncQueue().then(refresh);});
window.addEventListener('offline',()=>{mode='offline';renderCollectionInputGuard();traceContext=null;renderBatchDisposition();renderBatchPage();status('设备离线：现有快照可读，新操作进入本地队列。');});
window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();});
window.addEventListener('pagehide',()=>{disposed=true;operationsView?.clear();document.removeEventListener('input',watchCollectionInput,true);window.removeEventListener('beforeunload',protectCollectionExit);workspaces.dispose();lastOverview=null;qualityCheck.dispose();acceptanceReview.dispose();receiptReuse.dispose();clearTimeout(timer);resize.disconnect();chart?.dispose();heatmap?.dispose();geography?.dispose();database?.close();});
if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>status('离线静态缓存不可用，当前仍可在线使用。'));
if('PerformanceObserver' in window){try{new PerformanceObserver(list=>{const entry=list.getEntries().at(-1);$('performance').textContent=`本次页面绘制（LCP）：${Math.round(entry.startTime)} ms，仅代表当前设备与网络。`;}).observe({type:'largest-contentful-paint',buffered:true});}catch{}}
databaseReady.then(async()=>{
 try{const config=await api('/v1/auth/config');if(config.mode!=='local'){$('connect').textContent='机构账号登录';$('organization').closest('label').hidden=true;}}catch{}
 if(await transact('identity',s=>s.get('logoutPending'))){try{await api('/v1/session',{method:'DELETE'});await transact('identity',s=>s.delete('logoutPending'));}catch{}status('已退出，请主动连接会话。');return;}
 const saved=await transact('identity',s=>s.get('current'));
 try{const current=await api('/v1/me');if(saved&&saved.scopeHash!==current.scopeHash)clearVisible(true);await acceptActor(current);await syncQueue();await refresh();await loadData();timer=setTimeout(cycle,15000);}
 catch(error){if([401,403].includes(error.status)){await dropActor();status('请连接本地会话。');return;}if(validQueueActor(saved)){actor=saved;await loadCollection();$('organization').value=saved.organization||'Org1MSP';pendingProjection=scopedItems(await transact('queue',s=>s.getAll()),actor);const snapshot=await transact('snapshots',s=>s.get(snapshotKey()));if(usableSnapshot(snapshot))draw(snapshot.data,true);$('pending').textContent=String(scopedItems(await transact('queue',s=>s.getAll()),actor).length);renderQueue();}else{clearVisible();status('离线身份租期已过，请联网重新认证。');}}
});

let reviewItems=[];
let reviewLease=0, reviewGeneration=0;
async function selectReview(){const id=$('review-list').value,generation=++reviewGeneration;reviewLease=0;$('review-image').hidden=true;$('review-form').querySelector('button').disabled=true;if(!id)return;
 try{const lease=await api(`/v1/reviews/${encodeURIComponent(id)}/claim`,{method:'POST'});if(generation!==reviewGeneration)return;reviewLease=lease.expiresAt;$('review-image').src=`/v1/reviews/${encodeURIComponent(id)}/image`;$('review-image').hidden=false;$('review-form').querySelector('button').disabled=false;}catch(error){status(error.message);}
}
async function loadReviews(){const data=await api('/v1/reviews');reviewItems=data.items;$('review-list').replaceChildren(...data.items.map((r,i)=>{const option=document.createElement('option');option.value=r.id;option.textContent=`样本 ${i+1} · ${r.id.slice(0,10)}`;return option;}));$('review-state').textContent=`待复核 ${data.queued} · 已复核 ${data.reviewed}`;selectReview();}
action('load-reviews',loadReviews);$('review-list').addEventListener('change',selectReview);
submit('review-form',async()=>{const item=reviewItems.find(r=>r.id===$('review-list').value);if(!item)throw new Error('请先加载待复核样本');if(reviewLease*1000<=Date.now()){await selectReview();throw new Error('复核租期已刷新，请确认后提交');}await api(`/v1/reviews/${item.id}/label`,{method:'POST',body:JSON.stringify({requestId:crypto.randomUUID(),expectedRevision:item.revision,label:$('review-label').value,groupId:$('review-group').value,notes:''})});await loadReviews();});

function renderOutbox(data){
 outboxCursor=data.nextCursor;
 $('outbox-list').replaceChildren(...data.items.map(item=>{const o=document.createElement('option');o.value=item.id;o.dataset.deliveryStatus=item.status;o.dataset.attempt=String(item.attempt);o.textContent=`${item.id} · ${item.status==='outcome_unknown'||item.status==='dispatching'?'投递结果待核对':'待处理'} · 第 ${item.attempt} 次请求`;return o;}));
 const button=$('outbox-next');button.dataset.stateDisabled=String(!data.hasMore);button.disabled=!data.hasMore;updateOutboxActions();
}
async function loadOutbox(after=0){
 const scope=snapshotKey(),data=await api('/v1/outbox?after='+after);if(disposed||scope!==snapshotKey())return;
 if(!validOutboxPage(data)){clearOperations('运维信息暂不可核对，请检查连接后重新读取。');throw new Error('投递列表返回不完整，未替换为零条。');}
 renderOutbox(data);
}
function clearOperations(message){
 $('operations-state').textContent=message;$('incident-list').replaceChildren();$('ledger-job-list').replaceChildren();renderOutbox({items:[],nextCursor:0,hasMore:false});
}
function updateOutboxActions(){
 const option=$('outbox-list').selectedOptions[0],unknown=option&&['outcome_unknown','dispatching'].includes(option.dataset.deliveryStatus),operator=actor?.role==='operator';
 for(const [id,allowed] of [['retry-outbox',operator&&option&&!unknown],['outbox-confirm-published',operator&&unknown],['outbox-confirm-absent',operator&&unknown]]){const button=$(id);button.dataset.stateDisabled=String(!allowed);if(!button.hasAttribute('aria-busy'))button.disabled=!allowed;}
}
$('outbox-list').addEventListener('change',updateOutboxActions);
async function reconcileOutbox(outcome){
 const option=$('outbox-list').selectedOptions[0];if(!option||!['outcome_unknown','dispatching'].includes(option.dataset.deliveryStatus))throw new Error('请选择结果待核对的原事件');
 await api(`/v1/outbox/${encodeURIComponent(option.value)}/reconcile`,{method:'POST',body:JSON.stringify({note:$('incident-note').value,outcome,expectedAttempt:Number(option.dataset.attempt)})});await loadOperations();
}
action('outbox-confirm-published',()=>reconcileOutbox('published'));
action('outbox-confirm-absent',()=>reconcileOutbox('not-published'));
action('outbox-next',()=>loadOutbox(outboxCursor));
action('retry-outbox',async()=>{const id=$('outbox-list').value;if(!id)throw new Error('请选择待核对事件');await api(`/v1/outbox/${encodeURIComponent(id)}/retry`,{method:'POST',body:JSON.stringify({note:$('incident-note').value})});await loadOperations();});
async function loadOperations(){
 const view=ensureOperationsView(),ticket=view.begin();clearOperations('正在核对运维信息…');try{
 const outbox=await api('/v1/outbox?after=0');if(!view.current(ticket))return;
 const data=await api('/v1/operations');if(!view.current(ticket))return;
 const ledger=await api('/v1/ledger/status');if(!view.current(ticket))return;
 if(!validOutboxPage(outbox)||!validOperationsSummary(data)||!validLedgerStatus(ledger))throw new Error('运维返回不完整，请重新核对原记录。');
 renderOutbox(outbox);
 $('operations-state').textContent=`${data.ready?'运行检查通过':'有告警或待核对事件'} · 待投递事件 ${data.pendingEvents} · 需人工核对事件 ${data.actionRequiredEvents} · 待确认链操作 ${data.unconfirmedLedgerCommands}`;
 const names={database_unhealthy:'数据库检查异常',forecast_unhealthy:'预测任务异常',transport_unhealthy:'事件投递异常',outbox_unhealthy:'事件积压',ledger_unhealthy:'链上结果待核对',backup_unhealthy:'备份过期或未验证',model_drift:'识别分布变化，请复核'};
 $('incident-list').replaceChildren(...data.incidents.filter(i=>i.status!=='RESOLVED').map(i=>{const o=document.createElement('option');o.value=i.id;o.textContent=`${names[i.code]||i.code} · ${i.status==='ACKNOWLEDGED'?'已接手':'待接手'}`;return o;}));
 $('ledger-job-list').replaceChildren(...ledger.jobs.filter(j=>j.status!=='committed').map(j=>{const o=document.createElement('option');o.value=j.id;o.textContent=`${j.batch} · ${j.operation} · ${j.status}`;return o;}));
 view.render(data.workload,ticket);
 }catch(error){if(view.current(ticket)){clearOperations('运维信息暂不可核对，请检查连接后重新读取。');view.fail(ticket);}throw error;}
}
action('load-operations',loadOperations);
action('ack-incident',async()=>{const id=$('incident-list').value;if(!id)throw new Error('没有待处理告警');await api(`/v1/incidents/${id}/acknowledge`,{method:'POST',body:JSON.stringify({note:$('incident-note').value})});await loadOperations();});
action('retry-ledger-job',async()=>{const id=$('ledger-job-list').value;if(!id)throw new Error('没有待确认链操作');await api(`/v1/ledger/jobs/${id}/retry`,{method:'POST',body:JSON.stringify({note:$('incident-note').value})});await loadOperations();});

function renderTransformParents(){
 const select=$('transform-parents'),selected=new Set([...select.selectedOptions].map(option=>option.value));
 const rows=visibleBatches.filter(batch=>selected.has(batch.batchId)||batch.ownerMSP===actor?.organization&&['LANDED','PROCESSED'].includes(batch.state));
 for(const id of selected)if(!rows.some(batch=>batch.batchId===id))rows.push({batchId:id,unavailable:true});
 select.replaceChildren(...rows.map(batch=>{const option=document.createElement('option'),problem=transformParentProblem(batch,actor?.organization);option.value=batch.batchId;option.selected=selected.has(batch.batchId);option.disabled=Boolean(problem);option.textContent=problem?`${batch.batchId} · ${problem}`:`${batch.batchId} · ${batch.massGrams} 克${batch.localPending?' · 待同步':''}`;return option;}));
}
action('transform-clear-parents',()=>{for(const option of $('transform-parents').options)option.selected=false;renderTransformParents();status('父批次选择已清除。留空时使用当前批次，提交前仍会核对归属和状态。');});
action('add-output',()=>{if(document.querySelectorAll('.output-row').length>=20)throw new Error('单次最多20个产出批次');const row=document.createElement('div');row.className='output-row';row.innerHTML='<label>产出批次编号<input required pattern="[A-Za-z0-9_-]{1,128}"></label><label>产出（整数克）<input type="number" min="1" step="1" required></label><button type="button">移除此产出行</button>';row.querySelector('button').addEventListener('click',()=>row.remove());$('transform-outputs').append(row);});
async function loadTransfers(){const result=await api('/v1/transfers');transferRecords=result.transfers;const incoming=transferRecords.filter(t=>!t.used&&t.target===actor?.organization&&t.ownerMSP===t.source&&!['CONSUMED','RECALLED'].includes(t.state));$('transfer-list').replaceChildren(...incoming.map(t=>{const o=document.createElement('option');o.value=t.proposalId;o.textContent=`${t.batchId} · 来自${orgNames[t.source]} · ${t.proposalId.slice(0,12)}`;return o;}));$('proposal-id').value=$('transfer-list').value;$('transfer-state').textContent=`待接收 ${incoming.length} 条；已发起且待接收 ${transferRecords.filter(t=>!t.used&&t.source===actor?.organization).length} 条。`;}
action('load-transfers',loadTransfers);$('transfer-list').addEventListener('change',()=>{$('proposal-id').value=$('transfer-list').value;});
action('lookup-batch',()=>traceSelected($('lookup-id').value));action('verify-batch',()=>traceSelected());
// Resolve a hold online before retrying the dependent delivery; queuing behind it would deadlock.
submit('resolve-cold-form',async()=>{const issueId=$('cold-issue-list').value;if(!issueId)throw new Error('当前批次没有待处理冷链异常');const disposition=$('cold-disposition').value,reason=$('cold-reason').value;const key=[actor?.scopeHash,issueId,disposition,reason].join(':');const requestId=resolutionRequests.get(key)||crypto.randomUUID();resolutionRequests.set(key,requestId);if(resolutionRequests.size>100)resolutionRequests.delete(resolutionRequests.keys().next().value);const result=await api(`/v1/cold-issues/${issueId}/resolve`,{method:'POST',body:JSON.stringify({requestId,disposition,reason})});evidenceSelection={batchId:$('batch-list').value,evidenceId:result.evidenceId};currentEvidence=result.evidenceId;await transact('identity',s=>s.put(evidenceSelection,'evidence:'+actor.scopeHash));await refresh();await traceSelected();status('冷链处置已记录，处置证据待存证；若送达曾被拦截，可点“重试本批次送达”。');});
action('complete-evidence',()=>operateEvidence('complete'));
function download(path,name=''){const link=document.createElement('a');link.href=path;link.download=name;document.body.append(link);link.click();link.remove();}
action('queue-diagnostics',async()=>{
 const identity=actor;if(!validQueueActor(identity))throw new Error('请先连接有效会话。');
 let items;try{items=await transact('queue',s=>s.getAll(undefined,MAX_DIAGNOSTIC_INPUT+1),'readonly');}catch{throw new Error('本机队列暂不可读取，未生成诊断。');}
 if(actor!==identity||!validQueueActor(actor))throw new Error('会话已变化，未导出旧账户诊断。');
 const report=queueDiagnostics(items,identity),url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));
 const release=()=>URL.revokeObjectURL(url);addEventListener('pagehide',release,{once:true});
 try{download(url,'marine-queue-diagnostics.json');await new Promise(resolve=>setTimeout(resolve,0));}
 finally{removeEventListener('pagehide',release);release();}
 status('已导出本机同步诊断；仅含状态、分类和尝试次数，不代表服务端已经完成处理。');
});
action('download-evidence',()=>{if(!currentEvidence)throw new Error('请先选择当前批次的证据');download(`/v1/evidence/${currentEvidence}/content`);});
action('export-training',async()=>{await api('/v1/review-training-export?check=true');download('/v1/review-training-export');});
action('queue-rebase',async()=>{const item=await transact('queue',s=>s.get($('queue-list').value));if(!item||!scopedItems([item],actor).length)throw new Error('请选择当前账户的冲突记录');if(item.deliveryResult)throw new Error('业务已收件，请刷新工单后重试关联，不要重建原业务。');if(!/\/(transitions|transfers|accept)$/.test(item.path))throw new Error('这类记录请在对应上传或录入表单核对后重提，原文件仍保留。');const id=item.batchId||decodeURIComponent(item.path.match(/^\/v1\/batches\/([^/]+)/)?.[1]||'');const trace=await api(`/v1/batches/${encodeURIComponent(id)}/trace`);let proposal=null;if(item.path.endsWith('/accept')){await loadTransfers();proposal=transferRecords.find(t=>item.path===`/v1/transfers/${t.proposalId}/accept`);}const replacement=item.path.endsWith('/transitions')?rebaseTransition(item,trace.batch,crypto.randomUUID()):rebaseTransfer(item,trace.batch,crypto.randomUUID(),actor,proposal);if(!confirm(`最新状态为${stateNames[trace.batch.state]}，版本${trace.batch.revision}。保留原记录并重建本次操作？`))return;await transact('identity',s=>s.put(item,'superseded:'+item.requestId));await transact('queue',s=>s.delete(item.requestId));await transact('queue',s=>s.put(replacement));await syncQueue();await refresh();});
function replaceRegistrationOptions(id,options){
 const select=$(id),selected=select.value||select.dataset.requestedSelection;
 select.replaceChildren(...options);
 if(selected){select.value=selected;if(!select.value)select.dataset.requestedSelection=selected;else delete select.dataset.requestedSelection;}
}
function renderDataEntryGuidance(){
 renderClassificationRegistration();
 const connected=validQueueActor(actor),prefix=dataEntryRefreshFailed?'刷新未完成，请重试。':'';
 for(const [formId,hintId,selectId,ready,missing] of [
  ['device-form','device-prerequisite','device-site','个已登记站点。','先保存观测站点。'],
  ['environment-form','environment-prerequisite','reading-device','台启用设备。','先登记并启用设备。']
 ]){
  const select=$(selectId),count=select.options.length,hint=$(hintId),message=prefix+(!connected?'先连接工作台。':!dataEntryLoaded?'请刷新站点、设备与来源观测。':count?(select.value?`${count} ${ready}`:'原选择已不可用，请重新选择。'):missing);
  if(hint.textContent!==message)hint.textContent=message;
  const button=$(formId).querySelector('button:not([type=button])');button.dataset.stateDisabled=String(!connected||!dataEntryLoaded||!select.value);
  if(!button.hasAttribute('aria-busy'))button.disabled=button.dataset.stateDisabled==='true';
 }
 const fish=sourceEvents.filter(event=>event.eventType==='fish.observation').length,environment=sourceEvents.filter(event=>event.eventType==='environment.reading').length;
 const message=prefix+(!connected?'先连接工作台。':!dataEntryLoaded?'请先刷新来源观测。':`本页有 ${fish} 条鱼类、${environment} 条环境观测。`+(fish&&environment?'':'请切换站点或查看下一页。'));
 if($('catch-prerequisite').textContent!==message)$('catch-prerequisite').textContent=message;
}
for(const id of ['device-site','reading-device'])$(id).addEventListener('change',()=>{delete $(id).dataset.requestedSelection;renderDataEntryGuidance();});
function currentCollection(){const item=collectionState.items.find(item=>item.id===collectionState.activeId);return ownedCollection(item,actor)?item:null;}
function collectionButton(id,disabled){$(id).dataset.stateDisabled=String(disabled);$(id).disabled=disabled;}
function renderLegacyCollections(){
 const root=$('collection-legacy-list');root.replaceChildren();
 const rows=legacyCollectionState?.items||[];$('collection-legacy').hidden=!rows.length;
 for(const item of rows){const li=document.createElement('li'),details=document.createElement('details'),summary=document.createElement('summary');summary.textContent=`${item.id} · ${item.siteId} · ${item.records.length} 条本机记录${item.closed?' · 已结束':''}`;details.append(summary);details.addEventListener('toggle',()=>{if(!details.open||details.dataset.loaded)return;details.dataset.loaded='true';const list=document.createElement('ul');for(const source of item.sources){const row=document.createElement('li');row.textContent='本机来源 · '+source.eventId;list.append(row);}for(const record of item.records){const row=document.createElement('li');row.textContent=`${operationLabel(record.path)} · ${record.recordId} · ${record.state==='received'?'历史收件记录':record.state==='review'?'历史待复核记录':'历史记录'}`;list.append(row);}details.append(list);});li.append(details);root.append(li);}
}
function renderSavedCollections(){
 const preferred=$('collection-saved').value,items=[...collectionPage],current=currentCollection();
 if(current&&!items.some(item=>item.id===current.id))items.unshift(current);
 // A background metadata refresh must not erase a valid selection made before
 // the user presses Open. New-account and missing rows still reset normally.
 const selected=items.some(item=>item.id===preferred)?preferred:collectionState.activeId;
 const empty=document.createElement('option');empty.value='';empty.textContent='选择已保存的工单';
 $('collection-saved').replaceChildren(empty,...items.map(item=>{const option=document.createElement('option');option.value=item.id;option.textContent=`${item.siteId} · ${item.deviceId} · ${item.id}`;option.selected=item.id===selected;return option;}));
 collectionButton('collection-more',!collectionHasMore);
}
async function collectionPageLoad(after=''){
 const scope=actor?.scopeHash,result=await api('/v1/collection-orders?'+new URLSearchParams({after}));
 if(actor?.scopeHash!==scope)return;
 collectionPage=result.items;collectionCursor=result.nextCursor;collectionHasMore=result.hasMore;renderSavedCollections();
}
function collectionInputContext(){if(!validQueueActor(actor))return null;const order=currentCollection();return {scopeHash:actor.scopeHash,orderId:order?.id||null,revision:order?.revision??null,siteId:order?.siteId||$('filter-site').value,deviceId:order?.deviceId||null};}
function renderCollectionInputGuard(){
 const value=collectionInputs.state(collectionInputContext()),root=$('collection-input-guard');lastInputFilters={site:$('filter-site').value,species:$('filter-species').value};root.hidden=!value.dirty;
 const signature=JSON.stringify([value,inputReviewBusy,mode==='offline',Boolean(currentCollection()?.readOnly)]);if(root.dataset.inputState===signature)return;root.dataset.inputState=signature;
 $('collection-input-title').textContent=value.changed?'先核对当前工单与来源':'当前录入尚未提交';
 const owner=value.owner?.orderId?'工单 '+value.owner.orderId+' · 版本 '+value.owner.revision:'未绑定工单的录入';
 $('collection-input-status').textContent=inputReviewBusy?'正在核对当前工单与来源，原文本和数量仍保留…':value.changed?(value.owner?.orderId&&value.owner.orderId!==value.current?.orderId?'原输入仍属于'+owner+'。请返回原工单继续，或明确放弃输入后重新填写。':'原输入仍属于'+owner+'。当前工单或来源信息已变化；核对后保留文本，重新选择所需来源再提交。'):'这些输入属于'+owner+'。切换前可以继续处理，或明确放弃这些未提交输入。';
 $('collection-input-review').textContent=value.owner?.orderId===null&&value.current?.orderId?'将未绑定输入用于当前工单，并重选来源':'核对最新信息，保留文本并重选来源';$('collection-input-review').hidden=!value.changed;collectionButton('collection-input-review',Boolean(value.owner?.orderId&&value.owner.orderId!==value.current?.orderId)||inputReviewBusy||mode==='offline'||!validQueueActor(actor)||Boolean(currentCollection()?.readOnly));collectionButton('collection-input-discard',inputReviewBusy);
}
function lockCollectionInputs(value){for(const id of protectedInputForms)$(id).inert=value;$('collection-source-picker').inert=value;}
function clearCollectionInputs(){for(const id of protectedInputForms){$(id).reset();const feedback=$(id).querySelector('.form-feedback');if(feedback){feedback.hidden=true;feedback.textContent='';}}for(const id of ['catch-sources','collection-sources'])for(const option of $(id).options)option.selected=false;collectionInputs.clear();const order=currentCollection();if(order)$('area').value=order.siteId;renderCollectionInputGuard();}
function approveInputSwitch(){if(inputReviewBusy)throw Error('正在核对输入，请稍后再切换。');return !collectionInputs.state(collectionInputContext()).dirty||window.confirm('当前有尚未提交的采集输入。放弃这些输入后切换吗？已保存的队列、回执与原工单仍保留。');}
function assertCollectionInputs(){try{collectionInputs.assert(collectionInputContext());}catch(error){$('collection-input-title').focus({preventScroll:true});$('collection-input-guard').scrollIntoView({block:'nearest'});throw error;}}
function watchCollectionInput(event){
 const id=event.target.id;if(id==='filter-site'||id==='filter-species'){
  const value=collectionInputs.state(collectionInputContext()),relevant=id==='filter-site'?value.dirty:value.forms.includes('catch-form');
  if(relevant&&!approveInputSwitch()){event.stopImmediatePropagation();event.target.value=id==='filter-site'?lastInputFilters.site:lastInputFilters.species;renderCollectionInputGuard();return;}
  if(relevant)clearCollectionInputs();lastInputFilters={site:$('filter-site').value,species:$('filter-species').value};return;
 }
 const form=event.target.closest?.('form')?.id;if(protectedInputForms.includes(form)||id==='collection-sources'){collectionInputs.touch(id==='collection-sources'?'sources':form,collectionInputContext());renderCollectionInputGuard();}
}
function protectCollectionExit(event){if(collectionInputs.state(collectionInputContext()).dirty){event.preventDefault();event.returnValue='';}}
document.addEventListener('input',watchCollectionInput,true);window.addEventListener('beforeunload',protectCollectionExit);
action('collection-input-discard',async()=>{if(window.confirm('放弃当前表单中尚未提交的输入？已保存的队列和回执保持。'))clearCollectionInputs();});
action('collection-input-review',async()=>{
 const epoch=++inputReviewEpoch,scope=actor?.scopeHash,id=currentCollection()?.id,version=collectionInputs.version;inputReviewBusy=true;lockCollectionInputs(true);renderCollectionInputGuard();
 try{const loaded=await loadData(0,true);if(!loaded||actor?.scopeHash!==scope||currentCollection()?.id!==id||collectionInputs.version!==version||currentCollection()?.readOnly)throw Error('当前工单或输入已变化，原文本保留，请重新核对。');for(const name of ['catch-sources','collection-sources'])for(const option of $(name).options)option.selected=false;collectionInputs.saved('sources',collectionInputs.ticket('sources'));collectionInputs.ack(collectionInputContext(),true);status('已核对当前工单；文本与数量保留，请重新选择来源后提交。');}
 finally{if(epoch===inputReviewEpoch){inputReviewBusy=false;lockCollectionInputs(false);renderCollectionInputGuard();}}
});
async function saveCollection(){
 const scope=actor?.scopeHash;if(!validQueueActor(actor))throw new Error('身份已失效，原记录保留。');
 await transact('identity',store=>store.put({schema:1,order:currentCollection()},'server-collection:'+scope));
 if(actor?.scopeHash===scope)renderCollection();
}
async function putServerCollection(raw,{select=false}={}){
 const scope=actor?.scopeHash,order=serverCollection(raw,actor);
 collectionPage=collectionPage.map(item=>item.id===order.id?order:item);
 if(select||collectionState.activeId===order.id){collectionState={schema:2,items:[order],activeId:order.id};await saveCollection();}
 if(actor?.scopeHash!==scope)return null;renderSavedCollections();return order;
}
async function openServerCollection(id){
 if(!id)return;if(inputReviewBusy)throw Error('正在核对当前工单，请稍后再切换。');
 const changing=id!==currentCollection()?.id,returning=Boolean(collectionInputs.state(collectionInputContext()).dirty&&collectionInputs.state(collectionInputContext()).owner?.orderId===id);
 if(changing&&!returning&&!approveInputSwitch()){$('collection-saved').value=currentCollection()?.id||'';return;}
 const inputs=collectionInputs.version,ticket=++collectionEpoch,scope=actor?.scopeHash;
 let raw;try{raw=await api('/v1/collection-orders/'+encodeURIComponent(id));}catch(error){if(ticket===collectionEpoch&&actor?.scopeHash===scope&&$('collection-saved').value===id)$('collection-saved').value=currentCollection()?.id||'';throw error;}
 if(ticket!==collectionEpoch||actor?.scopeHash!==scope)return;
 if(changing&&inputs!==collectionInputs.version){if($('collection-saved').value===id)$('collection-saved').value=currentCollection()?.id||'';throw Error('读取期间有新的未提交输入，当前工单与内容保留，请重新选择。');}
 // Freeze only the short cache/metadata commit, after the user-editable read.
 const commit=++inputReviewEpoch;inputReviewBusy=true;lockCollectionInputs(true);renderCollectionInputGuard();
 try{const order=await putServerCollection(raw,{select:true});if(!order||ticket!==collectionEpoch||actor?.scopeHash!==scope)return;if(changing&&inputs!==collectionInputs.version)throw Error('切换期间的新增输入仍保留，请返回原工单核对。');if(changing&&!returning)clearCollectionInputs();$('filter-site').value=order.siteId;$('reading-device').dataset.requestedSelection=order.deviceId;await loadData();renderCollection();}
 finally{if(commit===inputReviewEpoch){inputReviewBusy=false;lockCollectionInputs(false);renderCollectionInputGuard();}}
}

async function loadCollection(){
 const scope=actor?.scopeHash,ticket=++collectionEpoch;
 const [legacy,cached]=await Promise.all([transact('identity',s=>s.get('collection:'+scope)),transact('identity',s=>s.get('server-collection:'+scope))]);
 if(actor?.scopeHash!==scope||ticket!==collectionEpoch)return;
 legacyCollectionState=legacy?.schema===1&&Array.isArray(legacy.items)&&legacy.items.length<=8&&legacy.items.every(item=>ownedCollection(item,actor)&&Array.isArray(item.sources)&&item.sources.length<=100&&Array.isArray(item.records)&&item.records.length<=128)?legacy:null;
 renderLegacyCollections();collectionState={schema:2,items:[],activeId:null};
 if(cached?.schema===1&&cached.order){try{const order=serverCollection(cached.order,actor);collectionState={schema:2,items:[order],activeId:order.id};}catch{status('本机工单缓存需核对，原记录保留。');}}
 const current=currentCollection();if(current){$('filter-site').value=current.siteId;$('reading-device').dataset.requestedSelection=current.deviceId;}
 try{
  const [page,members]=await Promise.all([api('/v1/collection-orders'),api('/v1/collection-members')]);
  if(actor?.scopeHash!==scope||ticket!==collectionEpoch)return;
  collectionPage=page.items;collectionCursor=page.nextCursor;collectionHasMore=page.hasMore;collectionMembersAvailable=members.available;
  $('collection-members').replaceChildren(...members.members.map(member=>{const option=document.createElement('option');option.value=member.subject;option.label=`${{operator:'操作员',auditor:'审核员'}[member.role]} · ${orgNames[member.organization]||member.organization}`;return option;}));
  if(current){const raw=await api('/v1/collection-orders/'+encodeURIComponent(current.id));if(actor?.scopeHash!==scope||ticket!==collectionEpoch)return;await putServerCollection(raw,{select:true});}
 }catch(error){
  if(actor?.scopeHash!==scope||ticket!==collectionEpoch)return;
  if(error.status){collectionState={schema:2,items:[],activeId:null};status(error.message);}
  else if(current){mode='offline';if(!collectionInputs.state(collectionInputContext()).dirty){sourceEvents=current.sources;registeredDevices=[];const option=document.createElement('option');option.value=current.deviceId;option.textContent=current.deviceId+'（离线，待在线复核）';$('reading-device').replaceChildren(option);dataEntryLoaded=true;dataEntryRefreshFailed=true;renderDataEntryGuidance();}}
 }
 renderSavedCollections();renderCollection();
}
function collectionDeviceOptions(){const selected=$('collection-device').value;$('collection-device').replaceChildren(...registeredDevices.filter(d=>d.enabled&&d.siteId===$('collection-site').value).map(d=>{const option=document.createElement('option');option.value=d.deviceId;option.textContent=d.deviceId;return option;}));if([...$('collection-device').options].some(o=>o.value===selected))$('collection-device').value=selected;}
function renderCollectionOptions(sites){
 replaceRegistrationOptions('collection-site',sites.map(site=>{const option=document.createElement('option');option.value=site.siteId;option.textContent=site.name;return option;}));collectionDeviceOptions();
 const order=currentCollection(),selected=new Set([...$('collection-sources').selectedOptions].map(option=>option.value));
 $('collection-sources').replaceChildren(...sourceEvents.filter(event=>!order||event.siteId===order.siteId&&Date.parse(event.occurredAt)/1000>=order.startAt&&Date.parse(event.occurredAt)/1000<=order.endAt).map(event=>{const option=document.createElement('option');option.value=event.eventId;option.selected=selected.has(event.eventId)||Boolean(order?.sources.some(source=>source.eventId===event.eventId));option.textContent=(event.eventType==='environment.reading'?'环境':'鱼类')+' · '+event.occurredAt+' · '+event.deviceId;return option;}));
}
function renderCollectionJourney(){
 const value=collectionJourney({order:currentCollection(),actor,pending:pendingProjection,acceptance:acceptanceView,online:mode!=='offline'}),root=$('collection-journey');
 root.hidden=!value;if(!value){root.dataset.projection='';$('collection-journey-title').textContent='';$('collection-journey-state').textContent='';$('collection-journey-counts').replaceChildren();return;}
 const signature=JSON.stringify([value,Boolean(collectionWriting||syncing)]);if(root.dataset.projection===signature)return;root.dataset.projection=signature;
 $('collection-journey-title').textContent=value.title;$('collection-journey-state').textContent=value.detail;
 $('collection-journey-counts').replaceChildren();for(const [label,count]of [['关联来源',value.counts.sources],['已关联回执',value.counts.receipts],['本机待同步',value.counts.local],['收件待关联',value.counts.linking]]){const cell=document.createElement('div'),term=document.createElement('dt'),number=document.createElement('dd');term.textContent=label;number.textContent=String(count);cell.append(term,number);$('collection-journey-counts').append(cell);}
 $('collection-next-step').textContent=value.label;$('collection-next-step').href='#'+value.target;$('collection-next-step').hidden=Boolean(collectionWriting||syncing);
 $('collection-next-step').dataset.requestId=value.requestId||'';
}
$('collection-next-step').addEventListener('click',()=>{
 const requestId=$('collection-next-step').dataset.requestId;if(!requestId)return;
 const current=currentCollection();if(!current||!scopedItems(pendingProjection,actor).some(item=>item.requestId===requestId&&item.collectionId===current.id))return;
 $('queue-filter').value='';renderQueue();$('queue-list').value=requestId;renderQueue();
});
function renderCollection(){
 renderClassificationRegistration();
 renderCollectionInputGuard();renderCollectionJourney();
 receiptReuse.contextChanged();renderReceiptReuse(reuseView);acceptanceReview.contextChanged();
 const order=currentCollection(),connected=validQueueActor(actor),operator=connected&&actor.role==='operator';
 $('collection-form').hidden=Boolean(order)||!operator;
 const createButton=$('collection-form').querySelector('button');createButton.disabled=!operator||!dataEntryLoaded||!$('collection-device').value;createButton.dataset.stateDisabled=String(createButton.disabled);
 $('collection-active').hidden=!order;$('collection-acceptance').hidden=!order;const root=$('collection-timeline');root.replaceChildren();$('collection-handoffs').replaceChildren();
 renderSavedCollections();collectionButton('collection-new',!operator||Boolean(collectionWriting||syncing));collectionButton('collection-open',!connected);collectionButton('collection-refresh',!connected);
 if(!order){$('collection-window').textContent='';$('collection-context').textContent=connected?(operator?'选择已保存工单，或联网新建后离线录入。':'选择已有工单查看或确认交接。'):'连接后可打开采集工单。';$('acceptance-context').textContent='请先在采集与录入中打开工单。验收针对选定的工单和版本。';return;}
 $('collection-window').textContent='工单时间：'+new Date(order.startAt*1000).toLocaleString()+' — '+new Date(order.endAt*1000).toLocaleString();
 $('collection-source-picker').hidden=order.readOnly;$('collection-edit-actions').hidden=order.readOnly;$('collection-handoff-controls').hidden=order.state==='handed-over';
 const states={open:'进行中',closed:'已结束','handoff-pending':'等待接收','handed-over':'已交接'};
 $('collection-context').textContent=`${mode==='offline'?'离线缓存 · ':'服务器工单 · '}${order.id} · ${order.siteId} · ${order.deviceId} · ${states[order.state]} · 版本 ${order.revision}`;$('acceptance-context').textContent=$('collection-context').textContent;
 const labels={pending:'本机待同步',binding:'业务已收件，待关联工单',conflict:'关联待处理',received:'服务端已收件',review:'服务端已收件，业务待复核'};
 for(const source of order.sources){const li=document.createElement('li');li.textContent=`来源 ${source.eventId} · ${source.eventType==='environment.reading'?'环境观测':'鱼类观测'} · ${source.deviceId} · ${new Date(source.occurredAt).toLocaleString()}`;root.append(li);}
 for(const row of collectionTimeline(order,pendingProjection)){const li=document.createElement('li');li.textContent=`${{environment:'环境读数',catch:'渔获记录',batch:'批次收件'}[row.kind]||operationLabel(row.path)} · ${row.recordId} · ${labels[row.state]}${row.chainStatus?' · '+(row.chainStatus==='COMMITTED'?'链上已确认':'链上未确认'):''}${row.reusedFromOrderId?' · 复用自工单 '+row.reusedFromOrderId:''}`;root.append(li);}
 if(!root.children.length){const li=document.createElement('li');li.textContent='尚未关联来源或收件记录。';root.append(li);}
 const pending=collectionWriting||syncing||pendingProjection.some(item=>item.collectionId===order.id);
 collectionButton('collection-attach',order.readOnly||pending||mode==='offline');
 collectionButton('collection-close',order.readOnly||pending||mode==='offline');
 collectionButton('collection-handoff',!collectionMembersAvailable||!operator||order.createdBy!==actor.subject||order.organization!==actor.organization||!['open','closed'].includes(order.state)||!order.records.length||pending||mode==='offline');
 $('collection-handoff-hint').textContent=order.state==='handed-over'?'接收回执已保存，工单记录只读。':order.handoffs.some(item=>item.state==='pending'&&item.recipient.subject===actor.subject)?'核对来源与收件记录后确认接收。':collectionMembersAvailable?'本机待同步记录清零后可发起交接，接收人需独立确认。':actor.mode==='local'?'当前为单人本机模式；双人交接需两个已有的成员身份。':'联网后核对成员权限并发起交接。';
 const offered=order.handoffs.find(item=>item.state==='pending');
 collectionButton('collection-accept',!offered||offered.recipient.subject!==actor.subject||!['operator','auditor'].includes(actor.role)||mode==='offline');
 collectionButton('collection-cancel',!offered||order.createdBy!==actor.subject||!operator||mode==='offline');
 for(const item of order.handoffs){const li=document.createElement('li');li.textContent=`${item.fromSubject} → ${item.recipient.subject} · ${{pending:'待接收',accepted:'交接已确认',cancelled:'已撤回'}[item.state]}${item.receipt?' · '+new Date(item.receipt.acceptedAt*1000).toLocaleString():''}`;$('collection-handoffs').append(li);}
}
async function receiveCollection(item,result){
 if((item.collectionId||item.path==='/v1/collection-orders')&&actor?.scopeHash!==item.scopeHash){const error=new Error('身份已切换，原收件凭据保留。');error.status=409;throw error;}
 if(item.path==='/v1/collection-orders'){const raw=await api('/v1/collection-orders/'+encodeURIComponent(item.body.id));await putServerCollection(raw,{select:!currentCollection()});await collectionPageLoad();return;}
 if(!item.collectionId)return;
 if(item.collectionServer){
  const scope=actor?.scopeHash,latest=await api('/v1/collection-orders/'+encodeURIComponent(item.collectionId)),record=collectionResult(item,result);
  const response=await api('/v1/collection-orders/'+encodeURIComponent(item.collectionId)+'/records',{method:'POST',body:JSON.stringify({requestId:item.requestId,expectedRevision:latest.revision,path:item.path,recordId:record.recordId})});
  if(actor?.scopeHash!==scope)throw new Error('身份已切换，收件凭据保留。');
  await putServerCollection(response.order);return;
 }
 // Old local-only jobs remain local. Never infer a server order for historical data.
 const scope=actor?.scopeHash,legacy=await transact('identity',s=>s.get('collection:'+scope));
 const order=legacy?.items?.find(value=>value.id===item.collectionId);
 if(!ownedCollection(order,actor)||!Array.isArray(order.records)||order.records.length>128){const error=new Error('旧作业记录需核对，成功收件凭据已保留。');error.status=409;throw error;}
 const record=collectionResult(item,result),index=order.records.findIndex(row=>row.requestId===record.requestId);
 if(index<0){if(order.records.length>=128)throw new Error('旧作业记录已满，成功收件凭据已保留。');order.records.push(record);}else order.records[index]=record;
 order.revision++;await transact('identity',s=>s.put(legacy,'collection:'+scope));if(actor?.scopeHash===scope){legacyCollectionState=legacy;renderLegacyCollections();}
}
$('collection-site').addEventListener('change',()=>{collectionDeviceOptions();renderCollection();});
$('collection-device').addEventListener('change',renderCollection);
function renderReceiptReuse(value){
 reuseView=value;const scope=reuseScope(),panel=$('collection-reuse-panel'),sourceSelect=$('collection-reuse-order'),recordSelect=$('collection-reuse-record');panel.hidden=!value.available;
 const selected=sourceSelect.value,options=[{id:'',title:'选择原工单'},...collectionPage.filter(order=>order.id!==scope.targetId).map(order=>({id:order.id,title:order.id+' · '+order.siteId}))];
 if(value.source&&!options.some(row=>row.id===value.source.id))options.push({id:value.source.id,title:value.source.id+'（已读取）'});
 sourceSelect.replaceChildren(...options.map(row=>{const option=document.createElement('option');option.value=row.id;option.textContent=row.title;return option;}));if(options.some(row=>row.id===selected))sourceSelect.value=selected;
 const previous=recordSelect.value,records=value.source?.records||[],linked=new Set(scope.records.map(row=>row.requestId));
 recordSelect.replaceChildren(...records.map(row=>{const option=document.createElement('option');option.value=row.requestId;option.textContent=operationLabel(row.path)+' · '+row.recordId+(linked.has(row.requestId)?'（已关联）':'');option.disabled=row.state!=='received'||linked.has(row.requestId)||row.kind==='environment'&&value.source.deviceId!==scope.deviceId;return option;}));
 if(records.some(row=>row.requestId===previous))recordSelect.value=previous;
 const busy=['loading','linking'].includes(value.state)||Boolean(collectionWriting)||syncing||pendingProjection.some(item=>item.collectionId===scope.targetId),chosen=records.find(row=>row.requestId===recordSelect.value),eligible=chosen&&!linked.has(chosen.requestId)&&chosen.state==='received'&&!(chosen.kind==='environment'&&value.source.deviceId!==scope.deviceId);
 for(const [id,disabled] of [['collection-reuse-order',busy],['collection-reuse-load',busy||!sourceSelect.value],['collection-reuse-record',busy||!records.length],['collection-reuse-sources',busy],['collection-reuse-submit',busy||!eligible]])collectionButton(id,!value.available||disabled);
 $('collection-reuse-summary').textContent=value.source?`原工单版本 ${value.source.revision} · ${records.length} 条收件、${value.source.sourceCount} 条来源。未勾选同步来源时，当前工单必须已有关联所需来源。`:'';
 $('collection-reuse-state').textContent=value.message;
 if(value.state==='idle'){const feedback=$('collection-reuse-form').querySelector('.form-feedback');if(feedback){feedback.hidden=true;feedback.textContent='';}}
}
$('collection-reuse-order').addEventListener('change',()=>receiptReuse.resetSource());
$('collection-reuse-record').addEventListener('change',()=>renderReceiptReuse(reuseView));
action('collection-reuse-load',()=>receiptReuse.load($('collection-reuse-order').value));
submit('collection-reuse-form',async()=>{
 const order=currentCollection(),scope=actor?.scopeHash;if(!order||mode==='offline')throw Error('请保持目标工单可录入且在线。');await collectionNoPending(order);collectionWriting++;renderCollection();
 try{const latest=await api('/v1/collection-orders/'+encodeURIComponent(order.id));if(latest.id!==order.id)throw Error('目标工单响应不一致，请重新打开。');if(actor?.scopeHash!==scope||currentCollection()?.id!==order.id)return;
  await putServerCollection(latest);const result=await receiptReuse.reuse($('collection-reuse-record').value,$('collection-reuse-sources').checked);if(!result||actor?.scopeHash!==scope||currentCollection()?.id!==order.id)return;
  if(result.alreadyLinked)return '已从服务器核对到该收件，无需重复关联或重做业务；验收另行完成。';
  if(currentCollection().revision<=result.order.revision)await putServerCollection(result.order);
  return '已有收件已关联到当前工单；原业务与原工单保持，结束采集和独立验收另行完成。';
 }finally{collectionWriting--;renderCollection();}
});
action('collection-open',()=>openServerCollection($('collection-saved').value));
action('collection-more',()=>collectionPageLoad(collectionCursor));
action('collection-refresh',async()=>{await collectionPageLoad();const order=currentCollection();if(order)await openServerCollection(order.id);});
action('collection-new',async()=>{if(!approveInputSwitch())return;collectionEpoch++;collectionState={schema:2,items:[],activeId:null};await saveCollection();clearCollectionInputs();renderCollection();});
submit('collection-form',async()=>{
 if(currentCollection())throw new Error('请先选择“新建工单”。');
 const existing=scopedItems(await transact('queue',store=>store.getAll()),actor).find(item=>item.path==='/v1/collection-orders');
 if(existing){await syncQueue();return '已保留上次创建请求，请先核对同步结果，不重复创建。';}
 const input=createCollection(actor,{id:crypto.randomUUID(),siteId:$('collection-site').value,deviceId:$('collection-device').value,startAt:Date.parse($('collection-start').value)/1000,endAt:Date.parse($('collection-end').value)/1000},registeredDevices);
 const body=Object.fromEntries(['id','siteId','deviceId','startAt','endAt'].map(key=>[key,input[key]]));
 const result=await enqueueOperation('/v1/collection-orders',body);
 if(result.queued)return result.received?'工单已收件，加载确认待重试。':'创建请求已保留在本机，联网后继续。';
 const order=currentCollection();if(!order||order.id!==input.id)return '工单已保存，可从列表打开。';
 $('filter-site').value=order.siteId;$('reading-device').value=order.deviceId;$('reading-time').value=$('collection-start').value;$('catch-origin').value=$('collection-start').value;$('area').value=order.siteId;await loadData();await refresh();status('工单已保存，可以录入或关联来源。');return '工单已保存，可以录入或关联来源。';
});
action('collection-attach',async()=>{
 const order=currentCollection();if(!order)throw new Error('请先打开工单。');assertCollectionInputs();const sourceInput=collectionInputs.ticket('sources');
 const ids=[...$('collection-sources').selectedOptions].map(option=>option.value),events=ids.map(id=>sourceEvents.find(event=>event.eventId===id));
 if(!events.length||events.some(event=>!event))throw new Error('请选择当前可核对的来源。');
 attachCollectionSources(order,events);
 const response=await api('/v1/collection-orders/'+encodeURIComponent(order.id)+'/sources',{method:'POST',body:JSON.stringify({requestId:crypto.randomUUID(),expectedRevision:order.revision,sources:events.map(event=>({eventId:event.eventId,revision:event.revision}))})});
 const updated=await putServerCollection(response.order);if(!updated)return;collectionInputs.saved('sources',sourceInput);renderCollectionInputGuard();for(const option of $('catch-sources').options)option.selected=updated.sources.some(source=>source.eventId===option.value);status('来源已关联到服务器工单，未创建新观测。');
});
async function collectionNoPending(order){const pending=scopedItems(await transact('queue',s=>s.getAll()),actor);if(collectionWriting||syncing||pending.some(item=>item.collectionId===order.id))throw new Error('本工单还有待同步或在途操作，请先核对。');}
action('collection-close',async()=>{const order=currentCollection();if(!order)return;if(collectionInputs.state(collectionInputContext()).dirty)throw Error('还有未提交输入，请先保存，或明确放弃后再结束工单。');await collectionNoPending(order);const response=await api('/v1/collection-orders/'+encodeURIComponent(order.id)+'/close',{method:'POST',body:JSON.stringify({requestId:crypto.randomUUID(),expectedRevision:order.revision})});await putServerCollection(response.order);status('工单已结束，记录保留在服务器。');});
action('acceptance-preview',()=>acceptanceReview.preview($('acceptance-policy').value));
action('acceptance-history',()=>acceptanceReview.list());
action('acceptance-submit',async()=>{const order=currentCollection();if(!order)return;await collectionNoPending(order);await acceptanceReview.submit($('acceptance-policy').value);});
$('acceptance-policy').addEventListener('change',()=>acceptanceReview.reset());
for(const [id,decision]of [['acceptance-approve','approve'],['acceptance-reject','reject']])action(id,async()=>{const reason=$('acceptance-reason').value.trim();if(!reason)throw Error('请填写独立审核说明。');await acceptanceReview.decide(decision,reason);});
action('acceptance-certificate',async()=>{const packet=await acceptanceReview.certificate();if(!packet)return;const url=URL.createObjectURL(new Blob([JSON.stringify(packet,null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download='marine-acceptance-'+packet.certificate.assessment.id+'.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
action('collection-handoff',async()=>{const order=currentCollection();if(!order)return;if(collectionInputs.state(collectionInputContext()).dirty)throw Error('还有未提交输入，请先保存，或明确放弃后再交接。');await collectionNoPending(order);const response=await api('/v1/collection-orders/'+encodeURIComponent(order.id)+'/handoffs',{method:'POST',body:JSON.stringify({requestId:crypto.randomUUID(),expectedRevision:order.revision,recipient:$('collection-recipient').value.trim()})});await putServerCollection(response.order);status('交接已发起，等待指定接收人确认。');});
for(const [button,operation]of [['collection-accept','accept'],['collection-cancel','cancel']])action(button,async()=>{const order=currentCollection(),handoff=order?.handoffs.find(item=>item.state==='pending');if(!handoff)return;const response=await api('/v1/collection-orders/'+encodeURIComponent(order.id)+'/handoffs/'+encodeURIComponent(handoff.id)+'/'+operation,{method:'POST',body:JSON.stringify({requestId:crypto.randomUUID(),expectedRevision:order.revision})});await putServerCollection(response.order);status(operation==='accept'?'交接已确认，接收回执已保存。':'交接已撤回，原记录保留。');});

function dataResult(message){$('data-result').textContent=message;return message;}
async function refreshDataAfterSave(message,dashboard=false){
 try{if(dashboard)await refresh();await loadData();return dataResult(message);}
 catch{return dataResult(message+' 列表未更新，请刷新，无需再次提交。');}
}
renderDataEntryGuidance();
function resetCatchSourceFilters(){
 $('catch-source-query').value='';$('catch-source-type').value='all';$('catch-source-images').checked=false;
 renderCatchSources();
}
function currentSourceExport(){return sourceExport(sourceEvents,{query:$('catch-source-query').value,type:$('catch-source-type').value,imagesOnly:$('catch-source-images').checked},{siteId:$('filter-site').value,dataSource:sourceExportBinding?.dataSource});}
function renderSourceTable(){
 if(sourceEvents.length&&!sourceScopeMatches(sourceExportBinding,actor,$('filter-site').value)){$('source-export-rows').replaceChildren();$('source-export-state').textContent='站点或会话已变化，请刷新来源观测后查看与导出。';return;}
 const packet=currentSourceExport();$('source-export-rows').replaceChildren(...packet.rows.map(item=>{const tr=document.createElement('tr');for(const value of [item.eventId,item.eventType==='environment.reading'?'环境读数':'鱼类观测',item.occurredAt,item.deviceId]){const td=document.createElement('td');td.textContent=String(value);tr.append(td);}return tr;}));
 $('source-export-state').textContent=packet.rows.length?`当前筛选 ${packet.rows.length} 条；仅覆盖已载入来源。`:'当前筛选没有来源观测。';
}
function renderCatchSources(){
 renderSourceTable();
 const select=$('catch-sources'),selected=new Set([...select.selectedOptions].map(option=>option.value));
 const result=filterSourceEvents(sourceEvents,{query:$('catch-source-query').value,type:$('catch-source-type').value,imagesOnly:$('catch-source-images').checked},[...selected]);
 const retained=new Set(result.retainedSelected);
 select.replaceChildren(...result.rows.map(event=>{
  const option=document.createElement('option');option.value=event.eventId;option.selected=selected.has(event.eventId);
  option.textContent=`${event.eventType==='environment.reading'?'环境':'鱼类'} · ${event.occurredAt.slice(0,19)} UTC · ${event.deviceId}${retained.has(event.eventId)?'（已选，筛选外）':''}`;
  return option;
 }));
 $('catch-source-filter-state').textContent=`当前页匹配 ${result.matched} / ${sourceEvents.length} 条${retained.size?`；另保留 ${retained.size} 条筛选外已选来源`:''}。`;
 for(const id of ['catch-source-query','catch-source-type','catch-source-images','catch-source-reset'])$(id).disabled=!validQueueActor(actor);
}
for(const id of ['catch-source-query','catch-source-type','catch-source-images'])$(id).addEventListener('input',renderCatchSources);
$('catch-source-reset').addEventListener('click',resetCatchSourceFilters);
$('catch-sources').addEventListener('change',renderCatchSources);
function renderSourceImageDownload(){
 const selected=sourceEvents.some(event=>event.imageAvailable&&event.eventId===$('source-image-list').value),button=$('download-observation');
 button.dataset.stateDisabled=String(!selected);button.disabled=!selected;
 $('source-image-state').textContent=selected?'将下载当前选中的原图。':sourceEvents.some(event=>event.imageAvailable)?'请先选择要下载的原图。':'当前页没有可下载的原图。';
}
function renderSourceImages(){
 const select=$('source-image-list'),previous=select.value,none=document.createElement('option');none.value='';none.textContent='选择要下载的原图';
 const images=sourceEvents.filter(event=>event.imageAvailable);select.replaceChildren(none,...images.map(event=>{const option=document.createElement('option');option.value=event.eventId;option.textContent=`${event.deviceId} · ${event.occurredAt}`;return option;}));
 select.value=images.some(event=>event.eventId===previous)?previous:'';renderSourceImageDownload();
 if(previous&&!select.value)$('source-image-state').textContent='所选原图不在当前页，请重新选择。';
}
$('source-image-list').addEventListener('change',renderSourceImageDownload);
async function loadData(before=0,reviewInputs=false){qualityCheck.reset();const request=++dataRequest,identity=actor,site=$('filter-site').value;const current=()=>!disposed&&request===dataRequest&&actor===identity&&$('filter-site').value===site;try{const [sites,devices,sources]=await Promise.all([api('/v1/sites'),api('/v1/devices'),api('/v1/source-events?'+new URLSearchParams({siteId:site,before:String(before)}))]);if(!current())return false;if(collectionInputs.state(collectionInputContext()).dirty&&!reviewInputs){const changed=collectionInputs.state(collectionInputContext()).changed||inputMetadataKey(registeredDevices,sourceEvents,registeredSites)!==inputMetadataKey(devices.devices,sources.events,sites.sites);if(changed)collectionInputs.requireReview();else{dataEntryLoaded=true;dataEntryRefreshFailed=false;renderDataEntryGuidance();}renderCollectionInputGuard();return !changed;}replaceRegistrationOptions('device-site',sites.sites.map(s=>{const o=document.createElement('option');o.value=s.siteId;o.textContent=`${s.name} (${s.siteId})`;return o;}));replaceRegistrationOptions('reading-device',devices.devices.filter(d=>d.enabled).map(d=>{const o=document.createElement('option');o.value=d.deviceId;o.textContent=`${d.deviceId} · ${d.siteId}`;return o;}));sourceEvents=sources.events;sourceExportBinding={scopeHash:identity.scopeHash,siteId:site,dataSource:sites.sites.find(item=>item.siteId===site)?.dataSource};sourceCursor=sources.nextCursor||0;const more=$('source-more');more.dataset.stateDisabled=String(!sources.hasMore);more.disabled=!sources.hasMore;for(const id of ['catch-sources','cold-sources']){const selected=new Set([...$(id).selectedOptions].map(o=>o.value));$(id).replaceChildren(...sourceEvents.map(e=>{const o=document.createElement('option');o.value=e.eventId;o.textContent=`${e.eventType==='environment.reading'?'环境':'鱼类'} · ${e.occurredAt.slice(0,19)} UTC · ${e.deviceId}`;o.selected=selected.has(e.eventId);return o;}));if([...selected].some(v=>!sourceEvents.some(e=>e.eventId===v))){$('cold-manual').checked=false;status('站点或来源记录已变化，请重新核对关联；不会自动转为手工记录。');}}renderCatchSources();renderSourceImages();const chosenForecast=$('cold-forecast').value;$('cold-forecast').replaceChildren();const none=document.createElement('option');none.value='';none.textContent='不关联';$('cold-forecast').append(none);if(serverDashboard?.prediction&&serverDashboard.siteId===$('filter-site').value&&serverDashboard.speciesId===$('filter-species').value){const p=serverDashboard.prediction;const o=document.createElement('option');o.value=p.forecastId;o.textContent=`${p.dataSource==='synthetic'?'合成':'现场'}预测 ${p.point.toFixed(2)}${p.validUntil<Date.now()/1000?'（已过期，仅作历史留痕）':''} · ${p.forecastId.slice(0,12)}`;$('cold-forecast').append(o);if(chosenForecast===p.forecastId)o.selected=true;}registeredDevices=devices.devices;registeredSites=sites.sites;renderCollectionOptions(sites.sites);dataEntryLoaded=true;dataEntryRefreshFailed=false;renderDataEntryGuidance();renderCollection();return true;}catch(error){if(!current())return;dataEntryRefreshFailed=true;renderDataEntryGuidance();throw error;}}
action('load-data',loadData);
submit('site-form',async()=>{await api(`/v1/sites/${encodeURIComponent($('site-id').value)}`,{method:'PUT',body:JSON.stringify({name:$('site-name').value,longitude:Number($('site-lon').value),latitude:Number($('site-lat').value),dataSource:$('site-source').value})});return refreshDataAfterSave('站点已保存。',true);});
submit('device-form',async()=>{await api(`/v1/devices/${encodeURIComponent($('device-id').value)}`,{method:'PUT',body:JSON.stringify({siteId:$('device-site').value,enabled:true})});return refreshDataAfterSave('设备与站点已绑定。');});
submit('environment-form',async()=>{const receipt=await enqueueOperation('/v1/environment-readings',{requestId:crypto.randomUUID(),deviceId:$('reading-device').value,occurredAt:Date.parse($('reading-time').value)/1000,temperatureC:Number($('reading-temperature').value),salinityPsu:Number($('reading-salinity').value),turbidityNtu:Number($('reading-turbidity').value)});return receipt.queued?dataResult(receipt.received?'读数已保存到服务端，工单关联待重试。':'读数已保留在本机，请查看待同步操作。'):refreshDataAfterSave('读数已保存到服务端。');});
submit('catch-form',async()=>{const receipt=await enqueueOperation('/v1/catches',{requestId:crypto.randomUUID(),siteId:$('filter-site').value,speciesId:$('filter-species').value,originAt:Date.parse($('catch-origin').value)/1000,labelAvailableAt:Date.parse($('catch-confirmed').value)/1000,catchKg:Number($('catch-kg').value),effortHours:Number($('catch-effort').value),sourceEventIds:[...$('catch-sources').selectedOptions].map(o=>o.value),dataSource:$('catch-source').value});return dataResult(receipt.queued?(receipt.received?'渔获已保存到服务端，工单关联待重试。':'渔获记录已保留在待同步队列，请核对同步状态。'):`渔获记录已保存：${receipt.cpue.toFixed(2)} 千克/标准作业小时，已纳入成熟样本。`);});
submit('data-import-form',async()=>{const file=$('import-file').files[0];if(file.size>400*1024)throw new Error('文件超过400KiB，请拆分');const body=new FormData();body.append('file',file);body.append('kind',$('import-kind').value);const result=await api('/v1/data-import',{method:'POST',body,timeoutMs:45000});return refreshDataAfterSave(`已校验并导入 ${result.count??result.acceptedEventIds?.length??0} 条记录。`,true);});
action('forecast-status-button',async()=>{const s=await api('/v1/forecast-status?'+new URLSearchParams({siteId:$('filter-site').value,speciesId:$('filter-species').value}));const p=serverDashboard?.siteId===$('filter-site').value&&serverDashboard?.speciesId===$('filter-species').value?serverDashboard.prediction:null;$('forecast-detail').textContent=`成熟样本：${s.matureLabels}\n${s.message}\n当前发布模型：${s.modelVersion||'尚未发布'}${p?'\n该预测使用模型：'+p.modelVersion+'\n预测发布时间：'+new Date(p.publishedAt*1000).toLocaleString()+'\n有效截至：'+new Date(p.validUntil*1000).toLocaleString()+'\n来源事件：'+p.lineage.sourceEventIds.length+' 条\n预测编号：'+p.forecastId:''}`;});
action('record-classification',async()=>{
 const capture=lastClassification;if(!capture||capture.recorded)throw Error('请先识别一张尚未登记的图片');
 const assertOwner=()=>{if(lastClassification!==capture||!classificationRegistration(capture,classificationContext()).canRecord)throw Error('识别结果的归属与当前工单或设备不一致，请先核对；原图与结果保留。');};
 assertOwner();if(capture.file.size>256*1024)throw Error('观测证据上限256KiB，请使用较小裁剪图；当前原图和识别结果已保留');
 const saved=async result=>{
  if(result.sampleId!==capture.requestId)throw Error('登记回执编号与原请求不一致，请核对原回执；不会重新上传。');
  if(!['published','review_pending','pending_review','review_rejected'].includes(result.status))throw Error('原登记仍待处理，请稍后读取同一回执；原图和请求保持。');
  capture.recorded=true;capture.registrationStatus=result.status;renderClassificationRegistration();
  const message=result.status==='published'?'分类观测与原图已登记；此照片不计入鱼群数量。请在工单中明确选择此观测作为来源。':result.status==='review_rejected'?'原观测已被人工拒绝，原图与结果保留；不重新上传，不进入工单来源。':'分类观测等待人工复核，暂不进入预测；复核发布后可选择为工单来源。';
  await refreshDataAfterSave(message,true);
 };
 try{
  if(capture.event){
   let receipt;try{receipt=await api('/v1/edge-samples/'+encodeURIComponent(capture.requestId));}catch(error){if(error.status!==404)throw error;}
   assertOwner();if(receipt&&receipt.status!=='admitting'){await saved(receipt);return;}
  }
  const devices=await api('/v1/devices');assertOwner();
  const device=devices.devices.find(d=>d.deviceId===capture.owner.deviceId);
  if(!device?.enabled||device.siteId!==capture.owner.siteId)throw Error('设备已失效或站点已变化，原图与识别结果已保留。');
  const op=currentCollection(),r=capture.result,at=capture.event?.occurredAt||capture.observedAt;
  if(op)guardCollection(op,actor,'/v1/environment-readings',{deviceId:device.deviceId,occurredAt:Date.parse(at)/1000},devices.devices,sourceEvents);
  capture.event ||= {schemaVersion:'1.0',eventId:capture.requestId,eventType:'fish.observation',tenantId:actor.tenant,siteId:device.siteId,deviceId:device.deviceId,sequenceNo:Date.now(),occurredAt:at,receivedAt:at,clockUncertaintyMs:0,traceId:capture.requestId,data:{trackId:capture.requestId,speciesId:r.speciesId,confidence:r.confidence,countMethod:'classificationOnly',qualityScore:1,exposureSeconds:1,modelVersion:r.modelVersion,preprocessVersion:r.preprocessVersion,evidenceId:capture.requestId}};
  const body=new FormData();body.append('image',capture.file);body.append('event',JSON.stringify(capture.event));body.append('needs_review',String(r.needsReview));
  renderClassificationRegistration();const result=await api('/v1/edge-samples',{method:'POST',body,timeoutMs:45000});await saved(result);
 }finally{renderClassificationRegistration();}
});
action('retry-delivery',async()=>{const batchId=$('batch-list').value;const items=scopedItems(await transact('queue',s=>s.getAll()),actor);const item=items.find(i=>i.path===`/v1/batches/${encodeURIComponent(batchId)}/transitions`&&i.body.nextState==='DELIVERED'&&i.blocked);if(!item)throw new Error('此批次没有被拦截的送达请求');await transact('queue',s=>s.put({...item,blocked:false,next:0}));await syncQueue();await refresh();await traceSelected();});
action('download-review',()=>{const id=$('review-list').value;if(!id)throw new Error('请先载入并选择复核图像');download(`/v1/reviews/${id}/image`);});
action('download-observation',()=>{const id=$('source-image-list').value;if(!sourceEvents.some(event=>event.imageAvailable&&event.eventId===id))throw new Error('请先选择当前页的观测原图。');download(`/v1/observations/${encodeURIComponent(id)}/evidence`);});

action('source-more',async()=>{if($('source-image-list').value){$('source-image-state').textContent='翻页前请先取消原图选择，原选择不会自动替换。';throw new Error('请先取消原图选择后翻页。');}if(['catch-sources','cold-sources'].some(id=>$(id).selectedOptions.length))throw new Error('请先完成当前来源对应的操作，或取消来源选择后翻页。');await loadData(sourceCursor);});

$('filter-site').addEventListener('input',renderSourceTable);
$('fill-synthetic').addEventListener('click',()=>{
 if($('site-source').value==='field'){status('当前选择现场站点。请先切换到合成示例，避免混用数据来源。');return;}
 const values={'site-id':'synthetic-example-01','site-name':'合成示例站点','site-lon':'120','site-lat':'30','device-id':'synthetic-sensor-01','reading-temperature':'18.5','reading-salinity':'31.2','reading-turbidity':'2.5'};
 if(Object.keys(values).some(id=>$(id).value)&&!window.confirm('填入示例会替换这些表单中的未保存值，继续？'))return;
 for(const [id,value] of Object.entries(values)){$(id).value=value;$(id).dispatchEvent(new Event('input',{bubbles:true}));}
 const date=new Date(Date.now()-new Date().getTimezoneOffset()*60000);$('reading-time').value=date.toISOString().slice(0,16);
 for(const id of ['device-site','reading-device']){$(id).value='';$(id).dispatchEvent(new Event('change',{bubbles:true}));}renderDataEntryGuidance();
 dataResult('已填入合成数据，尚未保存。依次保存示例站点、登记设备，再选择已登记的示例设备保存读数。');$('site-id').focus();
});
for(const format of ['json','csv'])$('export-source-'+format).addEventListener('click',()=>{
 try{if(!validQueueActor(actor)||!dataEntryLoaded||dataEntryRefreshFailed||!sourceScopeMatches(sourceExportBinding,actor,$('filter-site').value))throw Error('请先连接并刷新来源观测，确认当前访问范围。');
 const packet=currentSourceExport();if(!packet.rows.length)throw Error('当前筛选没有可导出的来源观测。');
 const url=URL.createObjectURL(new Blob([format==='csv'?sourceCsv(packet):JSON.stringify(packet,null,2)],{type:format==='csv'?'text/csv;charset=utf-8':'application/json'}));const a=document.createElement('a');a.href=url;a.download='marine-source-page.'+format;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);$('source-export-state').textContent=`已导出当前页 ${packet.rows.length} 条来源摘要；不含原图或凭据。`;
 }catch(error){$('source-export-state').textContent=error.message;}
});
