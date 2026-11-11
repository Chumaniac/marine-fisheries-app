import test from 'node:test';import assert from 'node:assert/strict';
import {collectionJourney} from '../src/collection-journey.js';
const actor={scopeHash:'scope',tenant:'synthetic',subject:'operator',expiresAt:10000};
const order={id:'order',scopeHash:'scope',tenant:'synthetic',subject:'operator',state:'open',revision:3,sources:[],records:[],handoffs:[],readOnly:false};
const make=(changes={},pending=[],acceptance=null,online=true)=>collectionJourney({order:{...order,...changes},actor,pending,acceptance,online,now:1000});
test('scope and offline boundaries do not invent live acceptance',()=>{assert.equal(collectionJourney({order,actor:{...actor,scopeHash:'other'},pending:[],now:1000}),null);assert.equal(collectionJourney({order,actor:{...actor,expiresAt:0},pending:[],now:1000}),null);assert.match(make({},[],null,false).title,/离线缓存/);});
test('known business receipts waiting for association are separate from locally queued writes',()=>{
 const base={scopeHash:'scope',tenant:'synthetic',subject:'operator',collectionId:'order',path:'/v1/batches',createdAt:100};
 const value=make({},[{...base,requestId:'local'},{...base,requestId:'received',deliveryResult:{status:'accepted'},blocked:true},{...base,requestId:'foreign',scopeHash:'other'}]);
 assert.equal(value.counts.local,1);assert.equal(value.counts.linking,1);assert.equal(value.counts.manual,1);assert.equal(value.requestId,'received');assert.equal(value.target,'queue-list');assert.match(value.detail,/不重复录入/);
});
test('open and closed work lead to explicit navigation, with no implied mutation',()=>{assert.equal(make().target,'collection-sources');assert.equal(make({records:[{requestId:'r',state:'received'}]}).target,'collection-close');assert.equal(make({state:'closed'}).target,'acceptance-preview');assert.equal(make({readOnly:true}).target,'collection-timeline');});
test('only a loaded current approval for the exact order can be called accepted',()=>{
 const selected={assessment:{orderId:'order',orderRevision:3},decision:{decision:'approve'},current:{stale:false,outcome:'ready'}};
 assert.match(make({state:'closed'},[],{selected,scopeHash:'scope'}).title,/独立验收已通过/);
 assert.doesNotMatch(make({state:'closed'},[],{scopeHash:'foreign',selected}).title,/独立验收已通过/);
 for(const change of [{assessment:{orderId:'other',orderRevision:3}},{assessment:{orderId:'order',orderRevision:2}},{current:{stale:true,outcome:'ready'}},{current:{stale:false,outcome:'blocked'}}])assert.doesNotMatch(make({state:'closed'},[],{scopeHash:'scope',selected:{...selected,...change}}).title,/独立验收已通过/);
});
test('handoff next step depends on the named recipient',()=>{const changes={state:'handoff-pending',handoffs:[{state:'pending',recipient:{subject:'operator'}}]};assert.equal(make(changes).target,'collection-accept');assert.equal(make({...changes,handoffs:[{state:'pending',recipient:{subject:'someone-else'}}]}).target,'collection-handoffs');});
