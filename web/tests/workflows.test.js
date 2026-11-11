import test from 'node:test';
import assert from 'node:assert/strict';
import {projectBatches,rebaseTransition,rebaseTransfer,observationFreshness} from '../src/workflows.js';

test('offline creation and successive transitions use projected revisions',()=>{
 const actor={organization:'Org1MSP'};
 const queue=[{path:'/v1/batches',body:{batchId:'b',massGrams:10,speciesId:'saithe'}},{path:'/v1/batches/b/transitions',body:{expectedRevision:1,nextState:'LANDED'}},{path:'/v1/batches/b/transitions',body:{expectedRevision:2,nextState:'PROCESSED'}}];
 const [batch]=projectBatches([],queue,actor);
 assert.equal(batch.state,'PROCESSED');assert.equal(batch.revision,3);assert.equal(batch.localPending,true);
 // A lost response must not project the already-applied transition twice.
 assert.equal(projectBatches([{...batch,state:'LANDED',revision:2}],queue,actor)[0].revision,3);
});
test('offline cold excursion holds delivery and accepted handoff projects recipient ownership',()=>{
 const batch={batchId:'b',state:'IN_COLD_CHAIN',revision:3,ownerMSP:'Org1MSP'};
 const queue=[{path:'/v1/transfers/p/accept',batchId:'b',body:{expectedRevision:3}},{path:'/v1/batches/b/cold-chain',batchId:'b',body:{minimumC:-2,maximumC:8,allowedMinimumC:-3,allowedMaximumC:4}}];
 const [projected]=projectBatches([batch],queue,{organization:'Org2MSP'});
 assert.equal(projected.ownerMSP,'Org2MSP');assert.equal(projected.revision,4);assert.equal(projected.openColdIssues,1);assert.equal(batch.ownerMSP,'Org1MSP');
});
test('handoff reconciliation refuses changed recipients and preserves original request',()=>{
 const item={requestId:'old',path:'/v1/transfers/p/accept',body:{expectedRevision:2},lastStatus:409};
 const batch={state:'PROCESSED',revision:4,ownerMSP:'Org1MSP'},actor={organization:'Org2MSP'},proposal={source:'Org1MSP',target:'Org2MSP',used:false};
 assert.equal(rebaseTransfer(item,batch,'new',actor,proposal).body.expectedRevision,4);
 assert.equal(item.body.expectedRevision,2);
 assert.throws(()=>rebaseTransfer(item,batch,'new',actor,{...proposal,used:true}));
 assert.throws(()=>rebaseTransfer(item,batch,'new',actor,{...proposal,target:'Org1MSP'}));
 assert.throws(()=>rebaseTransfer({...item,lastStatus:0},batch,'new',actor,proposal));
});
test('split/merge projection preserves parents and makes output available offline',()=>{
 const input=[{batchId:'a',revision:2,state:'LANDED',ownerMSP:'Org2MSP',speciesId:'saithe',massGrams:600},{batchId:'b',revision:4,state:'PROCESSED',ownerMSP:'Org2MSP',speciesId:'saithe',massGrams:400}];
 const out=projectBatches(input,[{path:'/v1/batch-transforms',body:{parents:{a:2,b:4},outputs:[{batchId:'c',massGrams:900}],lossGrams:100}}],{organization:'Org2MSP'});
 assert.equal(out[0].state,'CONSUMED');assert.equal(out[1].revision,5);assert.equal(out[2].ownerMSP,'Org2MSP');assert.equal(input[0].state,'LANDED');
});
test('explicit conflict reconciliation preserves original and refuses impossible state',()=>{
 const item={requestId:'old',path:'/v1/batches/b/transitions',body:{requestId:'old',expectedRevision:1,nextState:'IN_COLD_CHAIN'},lastStatus:409,blocked:true};
 const result=rebaseTransition(item,{state:'PROCESSED',revision:3},'new');assert.equal(result.body.expectedRevision,3);assert.equal(item.body.expectedRevision,1);assert.equal(result.supersedes,'old');
 assert.throws(()=>rebaseTransition(item,{state:'RECALLED',revision:5},'new'));
});


test('observation status separates an empty scope from stale or unknown observations',()=>{
 assert.equal(observationFreshness({observationCount:0,lastEventAt:null,stale:true}),'当前范围暂无观测记录');
 assert.equal(observationFreshness({observationCount:3,lastEventAt:1700000000,stale:true}),'观测数据待更新');
 assert.equal(observationFreshness({observationCount:3,lastEventAt:1700000000,stale:false}),'观测近期更新');
 for(const data of [{observationCount:3,lastEventAt:null,stale:false},{lastEventAt:NaN,stale:false},{lastEventAt:1700000000},{}])assert.equal(observationFreshness(data),'观测时间待核对');
});

test('batch disposition keeps scoped queue, global totals and evidence boundaries separate',async()=>{
 const {batchDisposition}=await import('../src/workflows.js'),now=Date.now();
 const actor={tenant:'t',subject:'u',scopeHash:'s',expiresAt:now/1000+600};
 const trace={batch:{batchId:'b',state:'IN_COLD_CHAIN',revision:2},totalEvidence:205,totalLedgerCommands:205,openColdIssues:7,hasMoreEvidence:true,hasMoreLedgerCommands:true,hasMoreColdIssues:true};
 const own={tenant:'t',subject:'u',scopeHash:'s',batchId:'b',dependencies:['b'],blocked:true,lastStatus:409};
 Object.defineProperty(own,'body',{get(){throw Error('must not read request body')}});
 const result=batchDisposition({trace,queue:[own,{...own,tenant:'other'}],actor,batchId:'b',fresh:true},now);
 assert.equal(result.status,'attention');assert.equal(result.items.find(i=>i.code==='queue').title,'本机有 1 条未确认操作');
 assert.ok(result.items.find(i=>i.code==='cold').title.includes('7'));
 assert.ok(result.items.find(i=>i.code==='ledger').title.includes('205'));
 assert.ok(result.items.find(i=>i.code==='evidence').title.includes('205'));
 assert.ok(result.items.find(i=>i.code==='evidence').detail.includes('不代表'));
 assert.equal(result.items.find(i=>i.code==='pages').target,'trace-more-evidence');
 assert.ok(result.items.length<=7);
});
test('batch disposition refuses stale, changed-scope, incomplete or excessive snapshots',async()=>{
 const {batchDisposition}=await import('../src/workflows.js'),now=Date.now();
 const actor={tenant:'t',subject:'u',scopeHash:'s',expiresAt:now/1000+600};
 const trace={batch:{batchId:'b',state:'DELIVERED',revision:2},totalEvidence:1,totalLedgerCommands:0,openColdIssues:0,hasMoreEvidence:false,hasMoreLedgerCommands:false,hasMoreColdIssues:false};
 for(const change of [{fresh:false},{batchId:'other'},{trace:{...trace,openColdIssues:undefined}},{actor:{...actor,expiresAt:0}},{queue:Array(1001).fill(null)}]){
  const result=batchDisposition({trace,queue:[],actor,batchId:'b',fresh:true,...change},now);assert.equal(result.status,'unknown');assert.ok(!JSON.stringify(result).includes('已完成'));
 }
 const result=batchDisposition({trace,queue:[{tenant:'other',subject:'u',scopeHash:'s',batchId:'b',blocked:true}],actor,batchId:'b',fresh:true},now);
 assert.equal(result.status,'review');assert.equal(result.items.some(i=>i.code==='queue'),false);
 assert.ok(result.label.includes('已送达'));assert.ok(result.items.find(item=>item.code==='evidence').detail.includes('单独核验'));
});
