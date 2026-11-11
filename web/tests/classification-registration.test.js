import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import {classificationOwner,classificationRegistration} from '../src/classification-context.js';
const owner={scopeHash:'scope',orderId:'workorder',revision:1,siteId:'A',deviceId:'device'};
const ownership={classificationRegistration,classificationContext:()=>owner,renderClassificationRegistration:()=>{}};

// Exercise the actual registered handler, including its success/retry ordering.
const app=readFileSync(new URL('../src/app.js',import.meta.url),'utf8');
const refreshSource=app.slice(app.indexOf('function dataResult('),app.indexOf('renderDataEntryGuidance();',app.indexOf('function dataResult(')));
const handlerSource=app.slice(app.indexOf("action('record-classification'"),app.indexOf("action('retry-delivery'"));
for(const status of ['published','pending_review'])test(`classification ${status} remains saved when the list refresh fails`,async()=>{
 let handler,writes=0;
 const fields={'reading-device':{value:'device'},'data-result':{textContent:''}};
 const capture={file:new Blob(['synthetic'],{type:'image/png'}),requestId:'sample-test',owner,observedAt:'2026-11-01T01:00:00Z',recorded:false,result:{speciesId:'saithe',confidence:0.9}};
 const context={
  ...ownership,action:(name,callback)=>{handler=callback;},lastClassification:capture,
  $:id=>fields[id],FormData,Date,actor:{tenant:'demo',scopeHash:'scope'},
  currentCollection:()=>({id:'workorder'}),guardCollection:()=>{},sourceEvents:[],
  api:async path=>{if(path==='/v1/devices')return {devices:[{deviceId:'device',siteId:'A',enabled:true}]};assert.equal(path,'/v1/edge-samples');writes++;return {sampleId:capture.requestId,status};},
  receiveCollection:()=>{throw new Error('unsupported implicit workorder association');},
  loadData:async()=>{throw new Error('offline after successful save');},refresh:async()=>{},
 };
 runInNewContext(refreshSource+handlerSource,context);
 await handler();assert.equal(capture.recorded,true);assert.equal(writes,1);
 assert.match(fields['data-result'].textContent,status==='published'?/选择.*来源/:/人工复核/);
 await assert.rejects(handler(),/尚未登记/);assert.equal(writes,1);
});

function classificationPage(api){
 let handler;const fields={image:{files:[new File(['synthetic-original'],'original.png',{type:'image/png'})],listeners:{},addEventListener(name,callback){this.listeners[name]=callback;}},classification:{textContent:''}};
 const context={...ownership,classificationRequest:0,lastClassification:null,submit:(id,callback)=>{handler=callback;},$:id=>fields[id],api,FormData,Date,crypto:{randomUUID:()=> 'synthetic-classification'}};
 runInNewContext(app.slice(app.indexOf("submit('classify-form'"),app.indexOf('async function enqueueOperation(')),context);
 return {context,fields,run:()=>handler()};
}
test('a late classification cannot attach an old result to a newly selected image',async()=>{
 let finish;const view=classificationPage(()=>new Promise(resolve=>finish=resolve)),pending=view.run();
 view.fields.image.files=[new File(['synthetic-new'],'new.png',{type:'image/png'})];view.fields.image.listeners.change?.();
 finish({speciesId:'saithe',confidence:.9,modelVersion:'synthetic-only'});await pending;
 assert.equal(view.context.lastClassification,null);assert.equal(view.fields.classification.textContent,'图片已选择，请开始识别。');
});
test('a classification failure clears the running message and retains the selected image',async()=>{
 const view=classificationPage(async()=>{throw Error('synthetic unavailable')});const file=view.fields.image.files[0];
 await assert.rejects(view.run(),/synthetic unavailable/);assert.ok(!view.fields.classification.textContent.includes('正在'));assert.equal(view.fields.image.files[0],file);
});

test('a classification completed after a workorder change keeps its original ownership',async()=>{
 let finish;const view=classificationPage(()=>new Promise(resolve=>finish=resolve)),pending=view.run();
 view.context.classificationContext=()=>({...owner,orderId:'new-order'});
 finish({speciesId:'saithe',confidence:.9,modelVersion:'synthetic-only'});await pending;
 assert.equal(view.context.lastClassification.owner.orderId,'workorder');
 assert.equal(classificationRegistration(view.context.lastClassification,view.context.classificationContext()).canRecord,false);
});

test('selecting another image cannot replace an unresolved registration asset or start a second inference',async()=>{
 let calls=0;const view=classificationPage(async()=>{calls++;return {}}),capture={owner,event:{eventId:'fixed'},recorded:false,file:new Blob(['retained-original']),result:{speciesId:'saithe'}};
 view.context.lastClassification=capture;view.context.status=()=>{};view.fields.image.listeners.change();
 assert.equal(view.context.lastClassification,capture);await assert.rejects(view.run(),/原登记尚未确认/);assert.equal(calls,0);
});

test('the image upload limit is checked before a classification request',async()=>{
 let calls=0;const view=classificationPage(async()=>{calls++;throw Error('oversized request was sent');});
 view.fields.image.files=[new File([new Uint8Array(8*1024*1024+1)],'large.png',{type:'image/png'})];
 await assert.rejects(view.run(),/8\s?MB/);assert.equal(calls,0);
});

test('switching workorders cannot silently register an existing classification under the new context',async()=>{
 let handler,writes=0;
 const capture={file:new Blob(['synthetic']),requestId:'original-event',recorded:false,result:{speciesId:'saithe',confidence:.9},owner:{scopeHash:'scope',orderId:'order-a',revision:1,siteId:'A',deviceId:'device'}};
 const context={...ownership,classificationContext:()=>({...owner,orderId:'order-b'}),action:(id,fn)=>{handler=fn;},lastClassification:capture,$:()=>({value:'device'}),FormData,Date,actor:{tenant:'demo',scopeHash:'scope'},currentCollection:()=>({id:'order-b'}),guardCollection:()=>{},sourceEvents:[],api:async path=>{if(path==='/v1/devices')return {devices:[{deviceId:'device',siteId:'A',enabled:true}]};writes++;return {status:'published'};},refreshDataAfterSave:async()=>{}};
 runInNewContext(handlerSource,context);
 await assert.rejects(handler(),/归属|核对|工单/);assert.equal(writes,0);assert.equal(capture.recorded,false);
});

test('same-scope reuse retains the exact result and File but cannot rebind an attempted event',()=>{
 const capture={owner:classificationOwner(owner),file:new Blob(['one asset']),result:{speciesId:'saithe'},recorded:false};
 const target={...owner,orderId:'another-order',revision:2,deviceId:'second-device'};
 assert.equal(classificationRegistration(capture,target).canReuse,true);
 const originalFile=capture.file,originalResult=capture.result;capture.owner=classificationOwner(target);
 assert.equal(classificationRegistration(capture,target).canRecord,true);assert.equal(capture.file,originalFile);assert.equal(capture.result,originalResult);
 assert.equal(classificationRegistration(capture,{...target,scopeHash:'foreign'}).canReuse,false);
 capture.event={eventId:'immutable'};
 assert.equal(classificationRegistration(capture,owner).canReuse,false);assert.equal(classificationRegistration(capture,owner).canRecord,false);
 assert.equal(classificationRegistration(capture,{...target,revision:3}).canRecord,true,'new revision can read the same immutable original event');
});

test('changing devices during the device read blocks the registration before a write',async()=>{
 let handler,finish,writes=0,current=owner;
 const capture={owner:classificationOwner(owner),file:new Blob(['synthetic']),result:{speciesId:'saithe'},recorded:false};
 runInNewContext(handlerSource,{...ownership,classificationContext:()=>current,lastClassification:capture,action:(id,fn)=>{handler=fn;},api:path=>path==='/v1/devices'?new Promise(resolve=>finish=resolve):(writes++,Promise.resolve({status:'published'}))});
 const pending=handler();current={...owner,deviceId:'changed'};finish({devices:[{deviceId:'device',siteId:'A',enabled:true}]});
 await assert.rejects(pending,/归属/);assert.equal(writes,0);
});

test('an unknown accepted registration reads its receipt with no second upload or changed event',async()=>{
 let handler,posts=0;const event={eventId:'fixed-event',occurredAt:'2026-11-01T01:00:00Z'},capture={owner,file:new Blob(['synthetic']),requestId:'fixed-event',result:{},event,recorded:false};
 runInNewContext(handlerSource,{...ownership,lastClassification:capture,action:(id,fn)=>{handler=fn;},api:async path=>{if(path==='/v1/edge-samples/fixed-event')return {sampleId:'fixed-event',status:'published'};posts++;throw Error('unexpected upload');},refreshDataAfterSave:async()=>{}});
 await handler();assert.equal(posts,0);assert.equal(capture.recorded,true);assert.equal(capture.event,event);
});

for(const status of ['review_rejected','published'])test(`receipt identity is required when recovering ${status}`,async()=>{
 let handler,returnedId='different-id',posts=0;const capture={owner,file:new Blob(['synthetic']),requestId:'fixed-event',result:{},event:{eventId:'fixed-event'},recorded:false};
 runInNewContext(handlerSource,{...ownership,lastClassification:capture,action:(id,fn)=>{handler=fn;},api:async path=>{if(path==='/v1/edge-samples/fixed-event')return {sampleId:returnedId,status};posts++;throw Error('unexpected upload');},refreshDataAfterSave:async()=>{}});
 await assert.rejects(handler(),/编号/);assert.equal(capture.recorded,false);returnedId='fixed-event';await handler();assert.equal(capture.recorded,true);assert.equal(capture.registrationStatus,status);assert.equal(posts,0);
});
