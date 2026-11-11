import test from 'node:test';
import assert from 'node:assert/strict';
import {retryable,retryDelay,usableSnapshot,deliverPending,scopedItems} from '../src/network.js';

test('scope selection fails closed for incomplete and nonfinite identities',()=>{
 const unbound={requestId:'unbound'};
 assert.deepEqual(scopedItems([unbound],{},1000),[]);
 const actor={tenant:'A',subject:'u',scopeHash:'s',expiresAt:100};
 const item={...actor,requestId:'current'};
 for(const invalid of [{...actor,tenant:''},{...actor,subject:null},{...actor,expiresAt:Infinity},{...actor,expiresAt:NaN},{...actor,expiresAt:'100'}]){
  assert.deepEqual(scopedItems([item],invalid,1000),[]);
 }
});

test('invalid local retry metadata pauses without sending or overwriting failure evidence',async()=>{
 const item={requestId:'original-id',body:{note:'keep-original'},attempt:'unknown',next:0,createdAt:1,errorCode:'ORIGINAL_FAILURE',lastMessage:'preserve-original-message'};
 let saved,sent=0;
 await deliverPending([item],{now:1000,send:async()=>sent++,remove:async()=>assert.fail('must retain'),save:async value=>saved=value});
 assert.equal(sent,0);assert.equal(saved.blocked,true);assert.equal(saved.body,item.body);
 assert.equal(saved.attempt,'unknown');assert.equal(saved.errorCode,'ORIGINAL_FAILURE');assert.equal(saved.lastMessage,'preserve-original-message');
});

test('network failure persists original operation, recovery removes only acknowledged item',async()=>{
 const item={requestId:'stable-id',body:{requestId:'stable-id',batchId:'b'},next:0,attempt:0,blocked:false};
 let saved,removed=false;
 await deliverPending([item],{now:1000,random:()=>.5,send:async()=>{throw new Error('offline');},remove:async()=>{removed=true;},save:async value=>{saved=value;}});
 assert.equal(removed,false);assert.deepEqual(saved.body,item.body);assert.equal(saved.requestId,item.requestId);assert.equal(saved.attempt,1);
 await deliverPending([saved],{now:100000,send:async body=>{assert.equal(body.requestId,'stable-id');},remove:async id=>{removed=id==='stable-id';},save:async()=>assert.fail('unexpected retry')});
 assert.equal(removed,true);
});
test('permanent conflict is retained for manual resolution and is not retried',async()=>{
 let saved,calls=0;const item={requestId:'x',body:{},next:0,attempt:0};
 await deliverPending([item],{send:async()=>{const e=new Error();e.status=409;throw e;},remove:async()=>assert.fail(),save:async value=>{saved=value;}});
 assert.equal(saved.blocked,true);
 await deliverPending([saved],{now:Infinity,send:async()=>{calls++;},remove:async()=>{},save:async()=>{}});assert.equal(calls,0);
});
test('backoff bounded and Retry-After is respected',()=>{
 assert.equal(retryDelay(999,0,()=>1),300000);assert.equal(retryDelay(0,600,()=>0),600000);
 assert.equal(retryable(403),false);assert.equal(retryable(503),true);
});
test('expired and future-dated snapshots are not accepted',()=>{
 assert.equal(usableSnapshot({saved:100},200),true);
 assert.equal(usableSnapshot({saved:100},86400100),false);
 assert.equal(usableSnapshot({saved:300},200),false);
});
test('queued operations cannot cross account, role scope or expired lease',()=>{
 const actor={tenant:'A',subject:'user-A',scopeHash:'scope-A',expiresAt:100};
 const item={...actor,requestId:'a'};
 assert.equal(scopedItems([item],actor,1000).length,1);
 assert.equal(scopedItems([item],{...actor,tenant:'B',scopeHash:'scope-B'},1000).length,0);
 assert.equal(scopedItems([item],{...actor,subject:'other'},1000).length,0);
 assert.equal(scopedItems([item],actor,100001).length,0);
 assert.equal(scopedItems([{requestId:'legacy-unscoped'}],actor,1000).length,0);
});

test('dependent commands remain ordered across a failed earlier operation',async()=>{
 const sent=[];
 await deliverPending([{requestId:'create',body:1,attempt:0,next:0},{requestId:'transition',body:2,attempt:0,next:0}],{send:async body=>{sent.push(body);throw new Error('offline');},save:async()=>{},remove:async()=>{}});
 assert.deepEqual(sent,[1]);
 const actor={tenant:'A',subject:'u',scopeHash:'s',expiresAt:100};
 assert.deepEqual(scopedItems([{...actor,createdAt:2,id:'b'},{...actor,createdAt:1,id:'a'}],actor,0).map(x=>x.id),['a','b']);
});

test('one failing batch does not block independent batches',async()=>{
 const sent=[];
 await deliverPending([{requestId:'a',body:1,dependencies:['A'],attempt:0,next:0},{requestId:'b',body:2,dependencies:['A'],attempt:0,next:0},{requestId:'c',body:3,dependencies:['B'],attempt:0,next:0}],{send:async body=>{sent.push(body);if(body===1)throw new Error('offline');},save:async()=>{},remove:async()=>{}});
 assert.deepEqual(sent,[1,3]);
});

test('automatic retries stop after eight failures and preserve the original request',async()=>{
 const item={requestId:'stable',body:{batchId:'A'},attempt:7,next:0,createdAt:1};let saved;
 await deliverPending([item],{now:1000,send:async()=>{throw new Error('offline');},remove:async()=>assert.fail(),save:async value=>saved=value});
 assert.equal(saved.blocked,true);assert.equal(saved.errorCode,'RETRY_BUDGET_EXHAUSTED');assert.equal(saved.requestId,item.requestId);
});
test('old queued work requires manual review without blocking another batch',async()=>{
 const sent=[],saved=[];
 await deliverPending([{requestId:'old',body:1,dependencies:['A'],attempt:0,next:0,createdAt:1},{requestId:'new',body:2,dependencies:['B'],attempt:0,next:0,createdAt:90000000}],{now:90000001,send:async body=>sent.push(body),remove:async()=>{},save:async item=>saved.push(item)});
 assert.deepEqual(sent,[2]);assert.equal(saved[0].errorCode,'RETRY_WINDOW_EXPIRED');
});

test('a retained rejection in one persisted order does not block another order of the same operation type',async()=>{
 const first={requestId:'rejected',path:'/v1/catches',body:{requestId:'rejected'},collectionServer:true,collectionId:'one',dependencies:['/v1/catches','collection:one'],attempt:1,next:0,createdAt:1,blocked:true};
 const second={...first,requestId:'valid',body:{requestId:'valid'},collectionId:'two',dependencies:['/v1/catches','collection:two'],blocked:false};
 const sent=[];
 await deliverPending([first,second],{now:1000,send:async body=>sent.push(body.requestId),remove:async()=>{},save:async()=>assert.fail('original rejected record must stay unchanged')});
 assert.deepEqual(sent,['valid']);assert.deepEqual(first.dependencies,['/v1/catches','collection:one']);
 for(const changed of [{...second,collectionId:'one',dependencies:['/v1/catches','collection:one']},{...second,batchId:'shared',dependencies:['/v1/catches','collection:two','shared']},{...second,dependencies:['/v1/catches']}]){
  const blocked={...first,batchId:changed.batchId,dependencies:changed.batchId?['/v1/catches','collection:one','shared']:first.dependencies};let calls=0;
  await deliverPending([blocked,changed],{now:1000,send:async()=>calls++,remove:async()=>{},save:async()=>{}});assert.equal(calls,0);
 }
});
