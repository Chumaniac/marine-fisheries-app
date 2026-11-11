import test from 'node:test';import assert from 'node:assert/strict';
import {createCollectionInputGuard,inputMetadataKey} from '../src/collection-input.js';
const a={scopeHash:'scope',orderId:'A',revision:1,siteId:'site',deviceId:'device'},b={...a,orderId:'B'};
test('unsaved inputs stay owned by the exact order and revision',()=>{const g=createCollectionInputGuard();g.touch('environment-form',a);assert.equal(g.state(a).changed,false);assert.throws(()=>g.assert(b),/核对/);assert.equal(g.state(b).owner.orderId,'A');assert.equal(g.state({...a,revision:2}).changed,true);g.ack({...a,revision:2});assert.equal(g.state({...a,revision:2}).changed,false);assert.equal(g.state(a).dirty,1);});
test('cross-account acknowledgement never rebinds old input',()=>{const g=createCollectionInputGuard();g.touch('catch-form',a);assert.throws(()=>g.ack({...a,scopeHash:'other'}),/账户/);assert.equal(g.state(a).owner.scopeHash,'scope');});
test('a completed old save does not clear newer edits or another form',()=>{const g=createCollectionInputGuard();g.touch('environment-form',a);const old=g.ticket('environment-form');g.touch('environment-form',a);g.touch('catch-form',a);g.saved('environment-form',old);assert.equal(g.state(a).dirty,2);g.saved('environment-form',g.ticket('environment-form'));assert.equal(g.state(a).dirty,1);assert.deepEqual(g.state(a).forms,['catch-form']);});
test('metadata refresh requires explicit review without storing any form text',()=>{const g=createCollectionInputGuard();g.touch('sources',a);g.requireReview();assert.throws(()=>g.assert(a),/核对/);g.ack(a);g.assert(a);assert(!JSON.stringify(g.state(a)).includes('payload'));g.clear();assert.equal(g.state(b).dirty,0);});

test('another order never silently inherits input; unassigned input requires an explicit bind',()=>{const g=createCollectionInputGuard();g.touch('catch-form',a);assert.throws(()=>g.ack(b,true),/另一份工单/);g.clear();g.touch('catch-form',{...a,orderId:null,revision:null});assert.throws(()=>g.ack(a),/另一份工单/);g.ack(a,true);g.assert(a);});

test('new sites trigger explicit review while unsaved input remains owned and intact',()=>{
 const guard=createCollectionInputGuard(),context={...a,orderId:null,revision:null};guard.touch('environment-form',context);
 const site={siteId:'synthetic-new',name:'Synthetic site',longitude:120,latitude:30,dataSource:'synthetic'};
 const before=inputMetadataKey([],[],[]),after=inputMetadataKey([],[],[site]);assert.notEqual(before,after);
 guard.requireReview();assert.equal(guard.state(context).changed,true);assert.equal(guard.state(context).dirty,1);
 guard.ack(context);assert.equal(guard.state(context).changed,false);assert.equal(guard.state(context).dirty,1);
 assert.notEqual(after,inputMetadataKey([],[],[{...site,dataSource:'field'}]));
 assert.notEqual(after,inputMetadataKey([],[],[{...site,longitude:121}]));
});
