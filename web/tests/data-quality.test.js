import test from 'node:test';
import assert from 'node:assert/strict';
import {validQualityReport,createQualityCheck} from '../src/data-quality.js';
const scope={siteId:'site',speciesId:'saithe',scopeHash:'synthetic'};
function report(){return {schema:'marine.data-quality/v1',siteId:'site',speciesId:'saithe',state:'checked',checkedRecords:1,checkedBytes:500,partial:false,partialReason:null,counts:{fishSelected:1,fishOtherSpecies:0,environment:0,other:0},issues:{schemaInvalid:0,hashMismatch:0,identityMismatch:0,environmentIncomplete:0,sensorSuspect:0,sensorInvalid:0},findingRecords:0,findings:[],findingsTruncated:false,limits:{records:500,bytes:1048576,recordBytes:8192,findings:32},coverage:{recognizedSiteOnly:true,includesOtherSpecies:true,unattributableRecordsExcluded:true,physicalImagesChecked:false}};}
test('quality report validates scope, counts, coverage and bounded details',()=>{
 assert.equal(validQualityReport(report(),scope),true);
 for(const change of [r=>r.siteId='other',r=>r.checkedRecords=501,r=>r.counts.fishSelected=2,r=>r.findingRecords=1,r=>r.findings=[{}],r=>r.coverage.physicalImagesChecked=true,r=>r.limits.findings=100,r=>r.partialReason='all',r=>r.state='passed']){const r=report();change(r);assert.equal(validQualityReport(r,scope),false);}
});
test('scope reset aborts and ignores both late success and late failure',async()=>{
 for(const fail of [false,true]){
  let release,signal;const states=[];const pending=new Promise((resolve,reject)=>release=()=>fail?reject(new Error('old')):resolve(report()));
  const check=createQualityCheck({fetchReport:(_,s)=>{signal=s;return pending;},onState:value=>states.push(value)});
  const running=check.run(scope);check.reset();assert.equal(signal.aborted,true);release();await running;
  assert.equal(states.at(-1).state,'idle');assert.ok(!states.some(s=>s.state==='ready'||s.state==='error'));
 }
});
test('malformed responses fail closed and a retry can display valid metadata',async()=>{
 const states=[];let malformed=true;const check=createQualityCheck({fetchReport:async()=>malformed?{}:report(),onState:value=>states.push(value)});
 await check.run(scope);assert.equal(states.at(-1).state,'error');malformed=false;await check.run(scope);assert.equal(states.at(-1).state,'ready');
 check.dispose();const length=states.length;await check.run(scope);assert.equal(states.length,length);
});

test('quality progress and result keep the captured site and species without identity metadata',async()=>{
 let release,captured;const states=[],selected={...scope};
 const check=createQualityCheck({fetchReport:(value)=>{captured=value;return new Promise(resolve=>release=resolve);},onState:value=>states.push(value)});
 const pending=check.run(selected);
 assert.deepEqual(states.at(-1).scope,{siteId:'site',speciesId:'saithe'});
 selected.siteId='changed-after-click';selected.speciesId='other';
 assert.equal(captured.siteId,'site');release(report());await pending;
 assert.equal(states.at(-1).state,'ready');assert.deepEqual(states.at(-1).scope,{siteId:'site',speciesId:'saithe'});
 assert.equal(Object.hasOwn(states.at(-1).scope,'scopeHash'),false);
 check.reset();assert.equal(states.at(-1).state,'idle');assert.equal(states.at(-1).scope,undefined);
});
