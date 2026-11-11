import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createAcceptanceReview,validAssessment,verifyCertificate} from '../src/collection-acceptance.js';
const packet=JSON.parse(readFileSync(new URL('./fixtures/collection-certificate.json',import.meta.url),'utf8'));
const report=()=>structuredClone(packet.certificate.assessment);
const scope={scopeHash:'synthetic-scope',orderId:'workorder-synthetic',orderRevision:4,online:true};
const envelope=()=>({assessment:report(),version:1,decision:null,current:{stale:false,bindingSha256:'b'.repeat(64),outcome:'ready'}});

test('assessment scope, finite coverage and outcome stay consistent',()=>{
 assert.equal(validAssessment(report(),scope),true);
 for(const change of [r=>r.orderId='foreign',r=>r.coverage.sources=101,r=>r.coverage.physicalImagesChecked=true,r=>r.coverage.complete=false,r=>r.issues={sensorInvalid:1},r=>r.findings=[{code:'sensorInvalid',reference:'unbound'}],r=>r.limits.batches=100,r=>r.findingsTruncated=true]){const r=report();change(r);assert.equal(validAssessment(r,scope),false);}
});
test('late results cannot cross account/order changes and requests are aborted',async()=>{
 let current={...scope},release,signal;const states=[];
 const review=createAcceptanceReview({getScope:()=>current,request:(_,options)=>{signal=options.signal;return new Promise(resolve=>release=resolve);},onState:value=>states.push(value)});
 const active=review.preview('local');current={...scope,scopeHash:'other-account'};review.contextChanged();assert.equal(signal.aborted,true);release(report());await active;
 assert.equal(states.at(-1).state,'idle');assert.equal(states.at(-1).preview,null);assert.equal(states.at(-1).selected,null);
 review.dispose();const count=states.length;await review.list();assert.equal(states.length,count);
});
test('an uncertain write retains its request ID and errors remain visible until an explicit retry',async()=>{
 const states=[],bodies=[];let failed=true;
 const review=createAcceptanceReview({getScope:()=>scope,request:async(_,options)=>{bodies.push(JSON.parse(options.body));if(failed)throw Error('Synthetic connection lost; inspect saved records.');return envelope();},onState:value=>states.push(value)});
 await review.submit('local');assert.equal(states.at(-1).state,'error');assert.match(states.at(-1).message,/inspect/);
 failed=false;await review.submit('local');assert.equal(bodies.length,2);assert.equal(bodies[0].requestId,bodies[1].requestId);assert.equal(states.at(-1).selected.assessment.id,report().id);
});
test('only the selected current assessment supplies review bindings and stale evidence blocks a decision',async()=>{
 const requests=[],states=[];let stale=true;
 const review=createAcceptanceReview({getScope:()=>scope,request:async(path,options={})=>{requests.push({path,options});if(options.method==='POST')return {...envelope(),version:2,decision:{assessmentId:report().id,decision:'reject'}};return {...envelope(),current:{...envelope().current,stale}};},onState:value=>states.push(value)});
 await review.open(report().id);await review.decide('approve','Reviewed');assert.equal(requests.length,1);assert.equal(states.at(-1).state,'error');
 stale=false;await review.open(report().id);await review.decide('reject','Synthetic review');const body=JSON.parse(requests.at(-1).options.body);assert.equal(body.expectedAssessmentVersion,1);assert.equal(body.expectedRevision,4);assert.equal(body.evidencePolicy,'local');
 assert.equal(states.at(-1).items[0].decision,'reject');assert.equal(states.at(-1).items[0].version,2,'directory and selected review must describe the same decision');
});
test('Python JCS certificate verifies in the browser implementation and tampering is refused',async()=>{
 assert.equal((await verifyCertificate(structuredClone(packet),scope,report().id)).sha256,packet.sha256);
 const changed=structuredClone(packet);changed.certificate.decision.reason='Changed';await assert.rejects(verifyCertificate(changed,scope,report().id),/完整性/);
 await assert.rejects(verifyCertificate(packet,{...scope,orderId:'foreign'},report().id),/结构/);
 const deep=structuredClone(packet);let cursor=deep.certificate;for(let i=0;i<70;i++){cursor.extra={};cursor=cursor.extra;}await assert.rejects(verifyCertificate(deep,scope,report().id),/容量/);
});
