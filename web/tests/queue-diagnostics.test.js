import test from 'node:test';
import assert from 'node:assert/strict';
import {queueDiagnostics} from '../src/queue-view.js';

const now=100000;
const actor={scopeHash:'PRIVATE_SCOPE',tenant:'PRIVATE_TENANT',subject:'PRIVATE_SUBJECT',expiresAt:1000};
const operation=(id,extra={})=>({...actor,requestId:id,path:'/v1/batches/PRIVATE_BATCH/transitions',createdAt:now-100,
 dependencies:['PRIVATE_BATCH'],body:{requestId:id,note:'PRIVATE_BODY'},file:{name:'PRIVATE_FILE'},lastMessage:'PRIVATE_ERROR',attempt:0,next:0,...extra});

test('diagnostics disclose only authorized counts and fixed classifications',()=>{
 const items=[operation('PRIVATE_REQUEST',{blocked:true,lastStatus:409,attempt:2}),operation('PRIVATE_SECOND'),operation('PRIVATE_READY',{dependencies:['separate']}),operation('PRIVATE_OTHER',{tenant:'different'})];
 const before=JSON.stringify(items),report=queueDiagnostics(items,actor,now);
 assert.deepEqual(report.counts,{all:3,manual:1,waiting:1,ready:1});
 assert.equal(report.failureClasses.conflict,1);
 assert.equal(report.details[0].failureClass,'conflict');
 assert.equal(report.details[0].operation,'更新批次状态');
 assert.equal(report.details[0].attempts,2);
 assert.ok(!JSON.stringify(report).includes('PRIVATE_'));
 assert.equal(JSON.stringify(items),before,'diagnosis never edits or retries original records');
});
test('empty, expired and malformed scopes remain distinct',()=>{
 assert.equal(queueDiagnostics([],actor,now).counts.all,0);
 for(const identity of [null,{}, {...actor,expiresAt:0},{...actor,expiresAt:Infinity}])assert.throws(()=>queueDiagnostics([],identity,now),/有效会话/);
});
test('detail retention stays bounded while counts cover the current scope',()=>{
 const report=queueDiagnostics(Array.from({length:100},(_,i)=>operation('PRIVATE_'+i,{dependencies:[]})),actor,now);
 assert.equal(report.counts.all,100);assert.equal(report.details.length,32);assert.equal(report.detailsOmitted,68);
 assert.ok(new TextEncoder().encode(JSON.stringify(report,null,2)).length<=16*1024);
 assert.throws(()=>queueDiagnostics(Array.from({length:1001},()=>operation('PRIVATE')),actor,now),/容量/);
});
test('unknown or malformed metadata never becomes raw error evidence',()=>{
 const report=queueDiagnostics([operation('PRIVATE',{attempt:'PRIVATE_ATTEMPT',errorCode:'PRIVATE_CODE',lastStatus:'PRIVATE_STATUS'})],actor,now);
 assert.equal(report.details[0].attempts,null);assert.equal(report.details[0].failureClass,'invalid-metadata');assert.equal(report.details[0].state,'manual');
 assert.ok(!JSON.stringify(report).includes('PRIVATE_'));
 assert.equal(queueDiagnostics([operation('PRIVATE',{errorCode:'PRIVATE_CODE',lastStatus:'PRIVATE_STATUS'})],actor,now).details[0].failureClass,'not-recorded');
 const exhausted=queueDiagnostics([operation('PRIVATE',{errorCode:'RETRY_BUDGET_EXHAUSTED',attempt:8})],actor,now);
 assert.equal(exhausted.details[0].failureClass,'retry-budget-exhausted');
});
