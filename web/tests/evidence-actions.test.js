import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import * as network from '../src/network.js';

const source=readFileSync(new URL('../src/app.js',import.meta.url),'utf8');
const evidenceSource=source.slice(source.indexOf("action('monitor'"),source.indexOf('const resize='));
const completeSource=source.slice(source.indexOf("action('complete-evidence'"),source.indexOf('function download('));
const traceSource=source.slice(source.indexOf('async function traceSelected('),source.indexOf("action('trace',"));
function setup({state='UPLOADED',refreshFailure=false,send}={}){
 const handlers={},fields=new Map(),calls=[],refreshes=[],messages=[];
 const field=id=>{if(!fields.has(id))fields.set(id,{value:'',textContent:'',dataset:{},options:[],replaceChildren(...items){this.options=items;this.value=items[0]?.value||'';},append(item){this.options.push(item);}});return fields.get(id);};
 field('batch-list').value='b1';field('evidence-list').value='e1';field('trace-result').textContent='initial';
 const context={URLSearchParams,action:(id,fn)=>{handlers[id]=fn;},$ :field,currentEvidence:'e1',currentTrace:{batch:{batchId:'b1',revision:1},evidence:[{evidenceId:'e1',status:state}]},scope:'scope-a',traceContext:'scope-a',
  snapshotKey:()=>context.scope,renderBatchDisposition(){},describeResult:value=>JSON.stringify(value),status:text=>messages.push(text),
  api:async(path,options={})=>{calls.push({path,method:options.method||'GET'});return send?send(path,options):{evidenceId:'e1',status:'PIN_VERIFIED',chainStatus:'COMMITTED'};},
  traceSelected:async(id,page)=>{refreshes.push({id,page});if(refreshFailure)throw Error('read unavailable');},
 };
 runInNewContext(evidenceSource+'\n'+completeSource,context);
 return {handlers,field,context,calls,refreshes,messages};
}
for(const action of ['pin','anchor','verify','complete-evidence'])test(`${action} refreshes the selected evidence after a response`,async()=>{
 const app=setup();await app.handlers[action]();assert.equal(app.refreshes.length,1);assert.equal(app.refreshes[0].id,'b1');
});
test('a readback failure preserves the accepted operation without another write',async()=>{
 const app=setup({refreshFailure:true});await app.handlers.pin();
 assert.equal(app.calls.length,1);assert.equal(app.refreshes.length,1);assert.match(app.field('trace-result').textContent,/PIN_VERIFIED/);
 assert.ok(app.messages.some(text=>text.includes('刷新')&&text.includes('查询')));
});
test('late evidence completion does not replace another selected batch',async()=>{
 let complete;const app=setup({send:()=>new Promise(resolve=>complete=resolve)});
 const pending=app.handlers.pin();app.field('batch-list').value='other';app.context.currentEvidence='other-evidence';app.field('trace-result').textContent='new batch';
 complete({evidenceId:'e1',status:'PIN_VERIFIED'});await pending;
 assert.equal(app.field('trace-result').textContent,'new batch');assert.equal(app.refreshes.length,0);
});
test('a restored identifier without a visible selected evidence cannot submit',async()=>{
 const app=setup();app.field('evidence-list').value='';await assert.rejects(app.handlers.pin(),/查询|选择/);assert.equal(app.calls.length,0);
});
for(const [state,expected]of [['PIN_VERIFIED',['anchor','verify']],['SUBMITTED',['anchor','verify']],['COMMITTED',['verify']]])test(`complete evidence reuses known ${state} progress`,async()=>{
 const app=setup({state});await app.handlers['complete-evidence']();assert.deepEqual(app.calls.map(row=>row.path.split('/').at(-1)),expected);
});

test('trace refresh retains each paginated cursor and current server revision',async()=>{
 const app=setup();const paths=[];
 Object.assign(app.context,{traceRequest:0,traceCursors:{},visibleBatches:[{batchId:'b1',revision:1,state:'CAUGHT'}],evidenceSelection:null,actor:{scopeHash:'scope-a'},stateNames:{CAUGHT:'已捕捞'},document:{createElement:()=>({value:'',textContent:''})},transact:async()=>{},api:async path=>{
  paths.push(path);if(path.endsWith('ledger-verify'))return {verified:true};
  const after=Number(new URL('http://fixture'+path).searchParams.get('evidenceAfter')||0);
  return {batch:{batchId:'b1',revision:2,state:'CAUGHT'},evidence:[{evidenceId:'e'+(after+1),status:'UPLOADED'}],hasMoreEvidence:after<200,nextEvidenceCursor:after+100,ledgerCommands:[],hasMoreLedgerCommands:false,nextLedgerCursor:0,coldIssues:[],hasMoreColdIssues:false,nextIssueCursor:0};
 }});
 runInNewContext(traceSource,app.context);await app.context.traceSelected('b1');
 assert.equal(app.context.visibleBatches[0].revision,2,'fresh server metadata must update an unprojected batch');
 await app.context.traceSelected('b1','evidence');await app.context.traceSelected('b1','current');
 assert.equal(new URL('http://fixture'+paths.filter(path=>path.includes('/trace')).at(-1)).searchParams.get('evidenceAfter'),'100');
 app.context.visibleBatches[0]={batchId:'b1',revision:9,state:'PROCESSED',localPending:true};await app.context.traceSelected('b1');
 assert.equal(app.context.visibleBatches[0].revision,9,'pending local projection must remain intact');
});

test('API field errors identify the field without echoing input or raw objects',async()=>{
 const apiSource=source.slice(source.indexOf('async function api('),source.indexOf('function draw('));
 const context={actor:{scopeHash:'scope-a'},performance:{now:()=>0},AbortController,setTimeout,clearTimeout,FormData,failures:0,rtt:0,mode:'normal',dropActor:async()=>{},responseErrorMessage:network.responseErrorMessage,
  fetch:async()=>({ok:false,status:422,headers:new Headers(),json:async()=>({detail:[{loc:['body','outputs',0,'massGrams'],type:'int_from_float',msg:'SYNTHETIC_PRIVATE_INPUT',input:'SYNTHETIC_PRIVATE_INPUT'}]})})};
 runInNewContext(apiSource,context);
 await assert.rejects(context.api('/v1/batch-transforms'),error=>{assert.match(error.message,/重量/);assert.match(error.message,/整数/);assert.ok(!error.message.includes('SYNTHETIC_PRIVATE')&&!error.message.includes('[object Object]'));return true;});
});


test('an unreadable evidence response stops later mutations until state is queried',async()=>{
 const app=setup({send:async()=>null});
 await assert.rejects(app.handlers['complete-evidence'](),/返回|查询/);
 assert.equal(app.calls.length,1);assert.equal(app.refreshes.length,1);
});

test('an old unauthorized response does not clear a newly connected identity',async()=>{
 let reply,dropped=0;const apiSource=source.slice(source.indexOf('async function api('),source.indexOf('function draw('));
 const context={actor:{scopeHash:'old'},performance:{now:()=>0},AbortController,setTimeout,clearTimeout,FormData,failures:0,rtt:0,mode:'normal',responseErrorMessage:network.responseErrorMessage,fetch:()=>new Promise(resolve=>reply=resolve),dropActor:async()=>{dropped++;context.actor=null;}};
 runInNewContext(apiSource,context);const pending=context.api('/v1/dashboard');context.actor={scopeHash:'new'};
 reply({ok:false,status:401,headers:new Headers(),json:async()=>({detail:'Unauthorized'})});await assert.rejects(pending);
 assert.equal(dropped,0);assert.equal(context.actor.scopeHash,'new');
});
for(const change of ['logout','switch','pagehide'])test(`an in-flight poll does not reschedule after ${change}`,async()=>{
 let finish;const scheduled=[];const actor={scopeHash:'s',tenant:'t',subject:'u',expiresAt:Date.now()/1000+60};
 const context={actor,disposed:false,mode:'normal',timer:null,validQueueActor:network.validQueueActor,syncQueue:()=>new Promise(resolve=>finish=resolve),refresh:async()=>true,setTimeout:fn=>scheduled.push(fn),status:()=>{},dropActor:async()=>{context.actor=null;}};
 runInNewContext(source.slice(source.indexOf('async function cycle('),source.indexOf('function action(')),context);
 const pending=context.cycle();if(change==='pagehide')context.disposed=true;else context.actor=change==='logout'?null:{...actor,scopeHash:'new'};finish();await pending;assert.equal(scheduled.length,0);
});
test('an unexpected local storage failure pauses polling without an unhandled rejection',async()=>{
 const scheduled=[],messages=[];const context={actor:{scopeHash:'s',tenant:'t',subject:'u',expiresAt:Date.now()/1000+60},disposed:false,mode:'normal',timer:null,validQueueActor:network.validQueueActor,syncQueue:async()=>{throw Error('store unavailable')},refresh:async()=>true,setTimeout:fn=>scheduled.push(fn),status:value=>messages.push(value),dropActor:async()=>{}};
 runInNewContext(source.slice(source.indexOf('async function cycle('),source.indexOf('function action(')),context);
 await context.cycle();assert.equal(scheduled.length,0);assert.match(messages.at(-1),/存储|同步/);
});

test('evidence controls do not submit overlapping operations',async()=>{
 let finish;const app=setup({send:path=>path.endsWith('/pin')?new Promise(resolve=>finish=resolve):Promise.resolve({chainStatus:'COMMITTED'})});
 const pending=app.handlers.pin();
 try{await assert.rejects(app.handlers.anchor(),/进行|处理/);assert.equal(app.calls.length,1);}
 finally{finish({evidenceId:'e1',status:'PIN_VERIFIED'});await pending;}
 await app.handlers.verify();
});
