import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import * as workflow from '../src/workflows.js';

const source=readFileSync(new URL('../src/app.js',import.meta.url),'utf8');
const handlerSource=source.slice(source.indexOf("submit('transform-form'"),source.indexOf("submit('cold-form'"));
const batch=(batchId,changes={})=>({batchId,revision:2,state:'LANDED',ownerMSP:'Org2MSP',speciesId:'saithe',massGrams:500,...changes});
function page(batches,ids){
 let handler,writes=0,payload;
 const fields={'transform-parents':{selectedOptions:ids.map(value=>({value}))},'loss-mass':{value:'0'}};
 const context={...workflow,visibleBatches:batches,actor:{organization:'Org2MSP'},$:id=>fields[id],
  crypto:{randomUUID:()=> 'synthetic-transform'},document:{querySelectorAll:()=>[{querySelectorAll:()=>[{value:'output'}, {value:'1000'}]}]},
  submit:(_id,callback)=>{handler=callback},selectedBatch:()=>batches[0],
  enqueueOperation:(path,body)=>{writes++;payload=body;return {path,body}}};
 runInNewContext(handlerSource,context);
 return {run:()=>handler(),writes:()=>writes,payload:()=>payload,fields};
}

test('the actual multi-parent submit refuses one conflicting or unavailable parent before queueing',()=>{
 for(const bad of [batch('bad',{localConflict:true}),batch('bad',{ownerMSP:'Org1MSP'}),batch('bad',{state:'CONSUMED'}),batch('bad',{revision:NaN})]){
  const view=page([batch('good'),bad],['good','bad']);
  assert.throws(view.run,/冲突|机构|状态|版本/);assert.equal(view.writes(),0);
  assert.deepEqual(view.fields['transform-parents'].selectedOptions.map(option=>option.value),['good','bad']);
 }
 const missing=page([batch('good')],['good','missing']);assert.throws(missing.run,/不可用|不存在/);assert.equal(missing.writes(),0);
});
test('valid multi-parent payload keeps revisions and does not consume original metadata',()=>{
 const rows=[batch('a'),batch('b',{revision:4})],view=page(rows,['a','b']);view.run();
 assert.equal(view.writes(),1);assert.deepEqual(JSON.parse(JSON.stringify(view.payload().parents)),{a:2,b:4});
 assert.deepEqual(rows.map(row=>row.state),['LANDED','LANDED']);
});
test('a queued transform cannot project past an earlier parent conflict or consume only some parents',()=>{
 const rows=[batch('a'),batch('b')],queue=[{path:'/v1/batches/b/transitions',blocked:true,body:{expectedRevision:2,nextState:'PROCESSED'}},
  {path:'/v1/batch-transforms',body:{parents:{a:2,b:2},outputs:[{batchId:'out',massGrams:1000}],lossGrams:0}}];
 const projected=workflow.projectBatches(rows,queue,{organization:'Org2MSP'});
 assert.deepEqual(projected.map(row=>row.state),['LANDED','LANDED']);assert.equal(projected[1].localConflict,true);
 assert.equal(projected.some(row=>row.batchId==='out'),false);
});
test('blocked transforms mark every input without consuming it and applied server revisions are not failed again',()=>{
 const command={path:'/v1/batch-transforms',body:{parents:{a:2,b:2},outputs:[{batchId:'out',massGrams:1000}],lossGrams:0}};
 const blocked=workflow.projectBatches([batch('a'),batch('b')],[{...command,blocked:true}],{organization:'Org2MSP'});
 assert.ok(blocked.every(row=>row.state==='LANDED'&&row.localConflict));
 const applied=workflow.projectBatches([batch('a',{state:'CONSUMED',revision:3}),batch('b',{state:'CONSUMED',revision:3})],[command],{organization:'Org2MSP'});
 assert.ok(applied.every(row=>row.state==='CONSUMED'&&!row.localConflict));assert.equal(applied.length,2);
});
test('parent preparation enforces the backend institution, species and capacity contract',()=>{
 assert.throws(()=>workflow.prepareTransformParents([batch('a')],['a'],'Org1MSP'),/加工机构/);
 assert.throws(()=>workflow.prepareTransformParents([batch('a')],['a','a'],'Org2MSP'),/不同父批次/);
 assert.throws(()=>workflow.prepareTransformParents([],Array.from({length:101},(_,i)=>'batch-'+i),'Org2MSP'),/100/);
 assert.throws(()=>workflow.prepareTransformParents([batch('a'),batch('b',{speciesId:'other'})],['a','b'],'Org2MSP'),/物种/);
});
test('refresh retains unavailable selections as disabled options instead of silently changing the input set',()=>{
 const select={selectedOptions:[{value:'a'},{value:'missing'}],replaceChildren(...options){this.options=options}};
 const renderer=source.slice(source.indexOf('function renderTransformParents()'),source.indexOf("action('transform-clear-parents'"));
 runInNewContext(renderer+';renderTransformParents();',{...workflow,visibleBatches:[batch('a')],actor:{organization:'Org2MSP'},$:()=>select,document:{createElement:()=>({})}});
 const missing=select.options.find(option=>option.value==='missing');assert.equal(missing.selected,true);assert.equal(missing.disabled,true);assert.match(missing.textContent,/不可用/);
});
