import test from 'node:test';
import assert from 'node:assert/strict';
import {attachCollectionSources,createCollection,guardCollection,collectionTimeline,collectionResult,ownedCollection} from '../src/collection.js';
const actor={scopeHash:'scope',tenant:'synthetic',subject:'operator',expiresAt:Date.now()/1000+3600};
const devices=[{deviceId:'device',siteId:'site',enabled:true}];
const event={eventId:'event-1',eventType:'fish.observation',siteId:'site',deviceId:'device',occurredAt:new Date(1500*1000).toISOString()};
const create=()=>createCollection(actor,{id:'op',siteId:'site',deviceId:'device',startAt:1000,endAt:2000},devices);
test('one context links existing sources without copying data, then guards each explicit submission',()=>{
 const op=attachCollectionSources(create(),[{...event,data:{private:'must not be retained'}}]);
 assert.equal(op.sources.length,1);assert.equal('data' in op.sources[0],false);
 guardCollection(op,actor,'/v1/catches',{siteId:'site',originAt:1600,labelAvailableAt:1700,sourceEventIds:['event-1']},devices,[event]);
 assert.equal(op.records.length,0);
 for(const change of [{siteId:'other'},{occurredAt:new Date(2100*1000).toISOString()}])assert.throws(()=>attachCollectionSources(op,[{...event,...change}]));
 assert.throws(()=>guardCollection(op,actor,'/v1/environment-readings',{deviceId:'device',occurredAt:2100},devices,[]));
 assert.throws(()=>guardCollection(op,actor,'/v1/environment-readings',{deviceId:'other',occurredAt:1500},devices,[]));
 assert.throws(()=>guardCollection(op,actor,'/v1/catches',{siteId:'site',originAt:1500,sourceEventIds:['unknown']},devices,[]));
 assert.throws(()=>guardCollection(op,actor,'/v1/catches',{siteId:'site',originAt:1500,sourceEventIds:['event-1']},devices,[{...event,revision:2}]));
 assert.throws(()=>guardCollection(op,actor,'/v1/batches',{areaCode:'other'},devices,[]));
});
test('scope, expired identity, missing or disabled device stop the workflow',()=>{
 assert.throws(()=>createCollection(actor,{id:'op',siteId:'site',deviceId:'missing',startAt:1000,endAt:2000},devices));
 const op=create();assert.equal(ownedCollection(op,{...actor,scopeHash:'other'}),false);
 assert.throws(()=>guardCollection(op,{...actor,expiresAt:0},'/v1/batches',{},devices,[]));
 assert.throws(()=>guardCollection(op,actor,'/v1/batches',{},[{...devices[0],enabled:false}],[]));
});
test('bounded metadata, duplicate acknowledgments, pending and chain status remain distinct',()=>{
 const op=create();const item={requestId:'request',collectionId:'op',path:'/v1/batches',body:{batchId:'batch'}};
 op.records=[collectionResult(item,{batch:{batchId:'batch',revision:2,chainStatus:'NOT_SUBMITTED'}})];
 assert.equal(op.records[0].state,'received');assert.equal(op.records[0].chainStatus,'NOT_SUBMITTED');
 assert.equal(collectionTimeline(op,[item]).length,1);assert.equal(collectionTimeline(op,[{...item,blocked:true}])[0].state,'conflict');
 assert.equal(collectionTimeline(op,[{...item,collectionId:'other'}])[0].state,'received');
 assert.equal(collectionResult(item,{status:'review_pending'}).state,'review');
 assert.throws(()=>attachCollectionSources(op,Array.from({length:101},(_,i)=>({...event,eventId:'id-'+i}))));
 op.records=Array.from({length:128},()=>({requestId:'id'}));assert.throws(()=>guardCollection(op,actor,'/v1/batches',{areaCode:'site'},devices,[]));
});

test('saved source metadata remains usable after a server JSON key-order roundtrip',()=>{
 const original=attachCollectionSources(create(),[event]);
 const saved={...original,sources:original.sources.map(source=>Object.fromEntries(Object.entries(source).reverse()))};
 assert.doesNotThrow(()=>attachCollectionSources(saved,[event]));
 assert.doesNotThrow(()=>guardCollection(saved,actor,'/v1/catches',{siteId:'site',originAt:1600,labelAvailableAt:1700,sourceEventIds:['event-1']},devices,[event]));
 for(const change of [{revision:2},{deviceId:'other-device'},{eventType:'environment.reading'}]){
  assert.throws(()=>attachCollectionSources(saved,[{...event,...change}]));
  assert.throws(()=>guardCollection(saved,actor,'/v1/catches',{siteId:'site',originAt:1600,labelAvailableAt:1700,sourceEventIds:['event-1']},devices,[{...event,...change}]));
 }
});
