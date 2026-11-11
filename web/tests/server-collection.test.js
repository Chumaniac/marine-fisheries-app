import {test} from 'node:test';
import assert from 'node:assert/strict';
import {deliverPending} from '../src/network.js';
import {deliverKnownOnce,receiptSummary,serverCollection} from '../src/server-collection.js';

test('known business success is retained and only association retries',async()=>{
 const item={requestId:'request-1',path:'/v1/catches',body:{requestId:'request-1'}};let sends=0,links=0,saved;
 const actions={send:async()=>{sends++;return {labelId:'request-1',status:'received'};},persist:async value=>{saved=structuredClone(value);},link:async()=>{links++;if(links===1){const error=new Error('offline');error.status=0;throw error;}}};
 await assert.rejects(deliverKnownOnce(item,actions));assert.equal(saved.deliveryResult.result.labelId,'request-1');
 await deliverKnownOnce(saved,actions);assert.equal(sends,1);assert.equal(links,2);
});
test('changed request cannot reuse a saved successful response',async()=>{
 const item={requestId:'request-2',path:'/v1/catches',body:{value:1}};let sends=0;
 const actions={send:async()=>{sends++;return {count:1};},persist:async()=>{},link:async()=>{}};
 await deliverKnownOnce(item,actions);item.body.value=2;
 await assert.rejects(deliverKnownOnce(item,actions),/已收件/);assert.equal(sends,1);
});
test('receipt cache contains bounded identifiers and statuses, not raw data',()=>{
 const summary=receiptSummary({labelId:'label',rawImage:'private',token:'secret',payload:'x'.repeat(1000000),acceptedEventIds:Array.from({length:200},(_,i)=>'id-'+i),batch:{batchId:'a',payload:'private'}});
 assert.equal(summary.acceptedEventIds.length,128);assert.equal(summary.batch.payload,undefined);assert.equal(summary.token,undefined);assert.equal(summary.rawImage,undefined);
});
test('server snapshots remain scoped and recipients cannot edit owner records',()=>{
 const actor={tenant:'demo',subject:'receiver',organization:'Org2MSP',role:'operator',scopeHash:'scope',expiresAt:Date.now()/1000+600};
 const value={id:'workorder-test',startAt:0,endAt:1,schema:'marine.collection-order/v1',tenant:'demo',serverPersisted:true,createdBy:'creator',organization:'Org1MSP',state:'handoff-pending',revision:2,sources:[],records:[],handoffs:[]};
 assert.equal(serverCollection(value,actor).readOnly,true);assert.throws(()=>serverCollection({...value,tenant:'other'},actor));
});

test('retry scheduler does not erase the stored success after link failure',async()=>{
 const item={requestId:'request-3',path:'/v1/catches',body:{requestId:'request-3'},createdAt:1000,attempt:0,next:0};
 let stored=item,sends=0,links=0;
 const options={now:1001,random:()=>0,save:async value=>{stored=structuredClone(value);},remove:async()=>{stored=null;},send:async(body,row)=>deliverKnownOnce(row,{send:async()=>{sends++;return {labelId:'request-3'};},persist:async value=>{stored=structuredClone(value);},link:async()=>{links++;if(links===1)throw new Error('link offline');}})};
 await deliverPending([item],options);assert.ok(stored.deliveryResult);assert.equal(stored.attempt,1);
 await deliverPending([stored],options);assert.equal(stored,null);assert.equal(sends,1);assert.equal(links,2);
});
