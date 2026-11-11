import assert from 'node:assert/strict';
import test from 'node:test';
import {validOperationalWorkload,validOperationsSummary,validOutboxPage,validLedgerStatus} from '../src/operations-view.js';

test('operational metadata accepts only the current tenant and explicit unavailable counts',()=>{
 const data={schema:'marine.operational-workload/v1',tenant:'demo',readOnly:true,access:'restricted',observedAt:1,complete:false,capacity:[],admissions:{available:false,counts:null,items:[],truncated:false,reason:'role_required'}};
 assert(validOperationalWorkload(data,'demo'));
 assert(!validOperationalWorkload(data,'foreign'));
 assert(!validOperationalWorkload({...data,admissions:{...data.admissions,counts:{received:0}}},'demo'));
});

import {createOperationsView} from '../src/operations-view.js';
const limits={events:500000,outbox_active:100000,labels:10000,batches:10000,reviews:1000,review_bytes:268435456,edge_admissions:1000,edge_evidence:1000};
const fixture=()=>({schema:'marine.operational-workload/v1',tenant:'demo',readOnly:true,access:'allowed',observedAt:1,complete:true,capacity:Object.entries(limits).map(([key,limit])=>({key,used:key==='edge_admissions'?1:0,limit,unit:key==='review_bytes'?'bytes':'records',status:'ok',reason:null})),admissions:{available:true,counts:{received:1,review_pending:0,published:0,review_rejected:0,unknown:0},items:[{sampleId:'synthetic-sample',deviceId:'synthetic-device',status:'received',recordedAt:null,cause:'not_recorded',nextAction:'check_original_request'}],truncated:false,reason:null}});
function dom(){const doc={createElement(tag){return {tagName:tag,ownerDocument:doc,textContent:'',children:[],dataset:{},attributes:{},append(...nodes){this.children.push(...nodes)},replaceChildren(...nodes){this.children=nodes},setAttribute(name,value){this.attributes[name]=value}}}};return doc.createElement('div')}
const text=node=>[node.textContent,...node.children.map(text)].join(' ');

test('counts and unknown time use metadata; unsafe or mismatched snapshots are not success',()=>{
 const data=fixture();assert(validOperationalWorkload(data,'demo'));
 assert(!validOperationalWorkload({...data,tenant:'foreign'},'demo'));
 data.admissions.items[0].recordedAt=1;assert(!validOperationalWorkload(data,'demo'));
 const huge=fixture();huge.capacity[0].limit=999999999;assert(!validOperationalWorkload(huge,'demo'));
 const invalid=fixture();invalid.capacity[0].used=null;assert(!validOperationalWorkload(invalid,'demo'));
});

test('a readonly view renders a next step and clears old tenant/late responses',()=>{
 let actor={scopeHash:'one',tenant:'demo',subject:'operator',role:'operator'};const root=dom(),view=createOperationsView({root,getScope:()=>actor});
 const old=view.begin(),latest=view.begin();assert.equal(view.render(fixture(),old),false);assert.equal(view.render(fixture(),latest),true);
 assert.match(text(root),/先核对设备原请求/);assert.match(text(root),/没有可靠的受理时间/);assert.equal(root.dataset.state,'observed');
 actor={...actor,scopeHash:'two',tenant:'foreign'};view.clear();assert.equal(view.render(fixture(),latest),false);assert.doesNotMatch(text(root),/synthetic-sample/);
 const ticket=view.begin();assert.equal(view.render(fixture(),ticket),false);assert.equal(root.dataset.state,'unavailable');
});

test('failure and restricted roles never display zero usage or old success',()=>{
 const actor={scopeHash:'one',tenant:'demo',subject:'viewer',role:'viewer'},root=dom(),view=createOperationsView({root,getScope:()=>actor});let ticket=view.begin();assert.equal(view.render(fixture(),ticket),false);assert.equal(root.dataset.state,'unavailable');
 ticket=view.begin();assert.equal(view.fail(ticket),false);assert.match(text(root),/不能当作已处理/);assert.doesNotMatch(text(root),/尚有预算/);
});

test('legacy summaries never coerce missing counts, malformed lists or unknown delivery states into success',()=>{
 const summary={ready:true,pendingEvents:0,actionRequiredEvents:0,unconfirmedLedgerCommands:0,incidents:[]};assert(validOperationsSummary(summary));
 for(const key of ['pendingEvents','actionRequiredEvents','unconfirmedLedgerCommands']){const missing={...summary};delete missing[key];assert(!validOperationsSummary(missing));assert(!validOperationsSummary({...summary,[key]:'0'}));}
 assert(!validOperationsSummary({...summary,incidents:{}}));
 assert(validOutboxPage({items:[],nextCursor:0,hasMore:false}));assert(!validOutboxPage({items:[{id:'one',attempt:1}],nextCursor:1,hasMore:false}));
 assert(validLedgerStatus({jobs:[{id:'one',batch:'batch',operation:'create',status:'pending'}]}));assert(!validLedgerStatus({jobs:[{id:'one',batch:'batch',operation:'create',status:'unknown'}]}));
});
