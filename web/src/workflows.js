import {scopedItems,validQueueActor} from './network.js';
export const stateNames={CREATED:'已创建',LANDED:'已上岸',PROCESSED:'已加工',IN_COLD_CHAIN:'冷链运输中',DELIVERED:'已送达',RECALLED:'已召回',CONSUMED:'已拆分或合并'};
export const orgNames={Org1MSP:'捕捞机构',Org2MSP:'加工机构'};
export const transitions={CREATED:['LANDED','RECALLED'],LANDED:['PROCESSED','IN_COLD_CHAIN','RECALLED'],PROCESSED:['IN_COLD_CHAIN','RECALLED'],IN_COLD_CHAIN:['DELIVERED','RECALLED'],DELIVERED:['RECALLED'],RECALLED:[],CONSUMED:[]};
export function transformParentProblem(batch,organization){
 if(organization!=='Org2MSP')return '只有加工机构可以提交加工操作。';
 if(!batch||batch.unavailable)return '原选父批次已不可用，请核对后重新选择。';
 if(batch.ownerMSP!==organization)return '所选父批次不属于当前机构，请重新核对。';
 if(batch.localConflict)return '所选父批次有待处理冲突，请先核对同步队列。';
 if(!['LANDED','PROCESSED'].includes(batch.state))return '所选父批次状态已变化，不能用于加工。';
 if(!Number.isSafeInteger(batch.revision)||batch.revision<1)return '父批次版本无效，请刷新核对。';
 if(!Number.isSafeInteger(batch.massGrams)||batch.massGrams<1||batch.massGrams>1e12||typeof batch.speciesId!=='string'||!batch.speciesId)return '父批次重量或物种信息无效，请刷新核对。';
 return '';
}
export function prepareTransformParents(batches,ids,organization){
 if(!Array.isArray(ids)||!ids.length||ids.length>100||new Set(ids).size!==ids.length)throw Error('单次加工需要1至100个不同父批次。');
 const selected=ids.map(id=>{
  if(typeof id!=='string'||!/^[A-Za-z0-9_-]{1,128}$/.test(id))throw Error('父批次编号无效，请重新选择。');
  const row=batches.find(batch=>batch?.batchId===id),problem=transformParentProblem(row,organization);
  if(problem)throw Error(problem);
  return row;
 });
 if(new Set(selected.map(row=>row.speciesId)).size!==1)throw Error('合批输入的物种必须一致。');
 const total=selected.reduce((sum,row)=>sum+row.massGrams,0);
 return {parents:Object.fromEntries(selected.map(row=>[row.batchId,row.revision])),total};
}
export function projectBatches(batches,queue,actor){
 const records=new Map(batches.map(b=>[b.batchId,{...b}]));
 for(const item of queue){
  const b=item.body,path=item.path||'/v1/batches';if(!b)continue;
  if(path==='/v1/batches'&&!records.has(b.batchId))records.set(b.batchId,{...b,ownerMSP:actor?.organization||'Org1MSP',revision:1,state:'CREATED',localPending:true});
  const match=path.match(/^\/v1\/batches\/([^/]+)\/(transitions|transfers)$/);
  if(match){const batch=records.get(decodeURIComponent(match[1]));if(!batch)continue;if(item.blocked){batch.localConflict=true;continue;}if(b.expectedRevision===batch.revision){if(match[2]==='transitions'){if(!transitions[batch.state]?.includes(b.nextState)){batch.localConflict=true;continue;}batch.state=b.nextState;}batch.revision++;batch.localPending=true;}}
  if(path.endsWith('/cold-chain')&&!item.blocked){const batch=records.get(item.batchId);if(batch&&(b.minimumC<b.allowedMinimumC||b.maximumC>b.allowedMaximumC)){batch.openColdIssues=(batch.openColdIssues||0)+1;batch.localPending=true;}}
  if(path.match(/^\/v1\/transfers\/[^/]+\/accept$/)&&!item.blocked){const batch=records.get(item.batchId);if(batch&&batch.revision===b.expectedRevision){batch.ownerMSP=actor?.organization;batch.revision++;batch.localPending=true;}}
  if(path==='/v1/batch-transforms'){
   const entries=Object.entries(b.parents||{});if(!entries.length||entries.length>100)continue;
   const parents=entries.map(([id,rev])=>[records.get(id),rev]);
   if(item.blocked){for(const [parent]of parents)if(parent)parent.localConflict=true;continue;}
   // A newer server revision may already include this operation; never project it twice.
   if(!parents.every(([parent,revision])=>parent&&parent.revision===revision))continue;
   try{prepareTransformParents(parents.map(([parent])=>parent),entries.map(([id])=>id),actor?.organization);}catch{continue;}
   for(const [parent]of parents){parent.state='CONSUMED';parent.revision++;parent.localPending=true;}
   for(const output of b.outputs)if(!records.has(output.batchId))records.set(output.batchId,{...output,speciesId:parents[0][0].speciesId,areaCode:'processed',ownerMSP:actor?.organization,revision:1,state:'PROCESSED',localPending:true});
  }
 }
 return [...records.values()];
}
export function rebaseTransition(item,batch,newId){
 if(!item.path?.endsWith('/transitions')||item.lastStatus!==409)throw new Error('这条操作需要在对应表单核对后重新提交，原记录可保留。');
 if(!transitions[batch.state]?.includes(item.body.nextState))throw new Error(`当前为${stateNames[batch.state]||batch.state}，不能执行原状态变更。`);
 return {...item,requestId:newId,body:{...item.body,requestId:newId,expectedRevision:batch.revision},supersedes:item.requestId,attempt:0,next:0,blocked:false,lastStatus:0};
}
export function describeResult(value){
 if(value.contentVerified!==undefined)return `原件完整性：${value.contentVerified?'通过':'未通过'}\n远端副本：${value.ipfs?.verified?'通过':'未满足策略'}（${value.ipfs?.replicasVerified||0} 份）\n防篡改存证：${value.chainVerified?'已核验':'尚未核验'}\n状态：${value.chainStatus||'未知'}`;
 if(value.batch){const b=value.batch;return `批次：${b.batchId}\n状态：${stateNames[b.state]||b.state}${b.localPending?'（本机待同步）':''}\n所属机构：${orgNames[b.ownerMSP]||b.ownerMSP||'捕捞机构'}\n重量：${b.massGrams} 克\n版本：${b.revision}\n待提交链操作：${value.totalLedgerCommands??(value.ledgerCommands?value.ledgerCommands.length:'尚未查询')}\n冷链待处理异常：${value.openColdIssues??(value.coldIssues?value.coldIssues.filter(i=>i.status==='OPEN').length:'尚未查询')}\n${value.proposalId?'交接申请：'+value.proposalId+'\n':''}${value.evidence?'证据记录总数：'+(value.totalEvidence??value.evidence.length)+'，当前页 '+value.evidence.length+' 条':''}`;}
 if(value.proposalId)return `交接申请已保存：${value.proposalId}`;
 if(value.excursion!==undefined)return `冷链记录已保存。${value.excursion?'发现超限：批次暂停送达，请查询溯源并处理冷链异常。':'温度在本次填写的允许范围内。'}\n证据：${value.evidenceId}`;
 if(value.evidenceId)return `证据：${value.evidenceId}\n状态：${value.status==='COMMITTED'?'存证已确认':value.status==='RESOLVED'?'处置已记录，证据等待存证':value.status==='PIN_VERIFIED'?'远端副本已核验':'已保存，等待后续存证'}`;
 if(value.outputs)return `加工完成，产出：${value.outputs.join('、')}\n损耗：${value.lossGrams} 克；质量守恒校验通过。`;
 if(value.batchId)return describeResult({batch:value});
 return value.validationCode==='VALID'?`存证已确认\n交易：${value.txId}`:value.status==='checked'?`对账已完成，本次确认 ${value.committed} 条操作。`:value.status==='busy'?'正在对账，请稍后核对。':value.status==='unconfirmed'?'部分链上结果尚未确认，原请求已保留，请稍后核对。':JSON.stringify(value,null,2);
}

export function rebaseTransfer(item,batch,newId,actor,proposal=null){
 if(item.lastStatus!==409)throw new Error('只有明确冲突的操作可以重建；未知结果应先重试原请求核对。');
 if(['CONSUMED','RECALLED'].includes(batch.state))throw new Error('当前批次状态不支持交接。');
 if(item.path.endsWith('/accept')){if(!proposal||proposal.used||proposal.target!==actor.organization||batch.ownerMSP!==proposal.source)throw new Error('交接已变化，请刷新申请并核对所属机构。');}
 else if(!item.path.endsWith('/transfers')||batch.ownerMSP!==actor.organization)throw new Error('只有所属机构能重新发起交接。');
 return {...item,requestId:newId,body:{...item.body,requestId:newId,expectedRevision:batch.revision},supersedes:item.requestId,attempt:0,next:0,blocked:false,lastStatus:0};
}

export function observationFreshness(data){
 if(data?.observationCount===0&&data.lastEventAt===null)return '当前范围暂无观测记录';
 if(!Number.isFinite(data?.lastEventAt)||data.lastEventAt<=0||typeof data.stale!=='boolean')return '观测时间待核对';
 return data.stale?'观测数据待更新':'观测近期更新';
}

export function batchDisposition({trace,queue=[],actor,batchId,fresh=false},now=Date.now()){
 const items=[],add=(code,title,detail,target)=>items.push({code,title,detail,target});
 const unknown=detail=>{add('refresh','重新查询当前批次',detail,'trace');return {status:'unknown',label:'状态待核对',items};};
 if(!validQueueActor(actor,now))return unknown('请先连接当前账户会话。');
 if(!Array.isArray(queue)||queue.length>1000)return unknown('队列超出本次检查范围，请先查看同步诊断。');
 const own=scopedItems(queue,actor,now);
 if(own.length>100)return unknown('当前账户队列超出本次检查范围，请先查看同步诊断。');
 if(own.some(item=>typeof item.batchId!=='string'&&(!Array.isArray(item.dependencies)||!item.dependencies.length||item.dependencies.length>200)))return unknown('部分操作缺少可用的批次定位，请先查看同步诊断。');
 const pending=own.filter(item=>item.batchId===batchId||(Array.isArray(item.dependencies)&&item.dependencies.length<=200&&item.dependencies.includes(batchId)));
 if(pending.length)add('queue',`本机有 ${pending.length} 条未确认操作`,pending.some(item=>item.blocked||item.lastStatus===409)?'存在需人工核对的操作；原请求与失败记录仍保留。':'先核对同步进度，不重复提交同一操作。','queue-summary');
 const count=value=>Number.isSafeInteger(value)&&value>=0;
 if(!batchId||!fresh||trace?.batch?.batchId!==batchId||!count(trace.openColdIssues)||!count(trace.totalLedgerCommands)||!count(trace.totalEvidence)||
   !['hasMoreEvidence','hasMoreLedgerCommands','hasMoreColdIssues'].every(key=>typeof trace[key]==='boolean'))return unknown('当前账户、批次或版本尚未取得有效溯源快照；不会显示完成结论。');
 if(trace.openColdIssues)add('cold',`有 ${trace.openColdIssues} 条冷链异常待处置`,'由所属机构核对异常和处置记录，核对前不要确认送达。','resolve-cold-form');
 if(!trace.totalLedgerCommands&&trace.ledgerVerification!=='verified')add('ledger','存证状态仍需核对','尚未取得本次链上核对结论，本地记录不代表确认。','ledger-result');
 if(trace.totalLedgerCommands)add('ledger',`有 ${trace.totalLedgerCommands} 条存证操作待确认`,'本地保存不等于链上确认，先查看原请求与存证回执。','ledger-result');
 add('evidence',trace.totalEvidence?`核对 ${trace.totalEvidence} 条已登记证据`:'尚未登记原始证据',trace.totalEvidence?'登记条数不代表原件完整性通过，请单独核验所选原件与存证。':'选择已有成功原件并核对关联；不重新生成证据。',trace.totalEvidence?'verify':'evidence-form');
 const more=[['hasMoreEvidence','trace-more-evidence'],['hasMoreLedgerCommands','trace-more-ledger'],['hasMoreColdIssues','trace-more-issues']].find(([key])=>trace[key]);
 if(more)add('pages','记录还有后续页','上面的汇总覆盖整个批次；逐条核对需要继续翻页。',more[1]);
 if(['RECALLED','CONSUMED'].includes(trace.batch.state))add('state',stateNames[trace.batch.state],'此状态限制后续流转，先核对已保存的业务记录。','trace-result');
 return {status:pending.length||trace.openColdIssues||trace.totalLedgerCommands?'attention':'review',label:`记录状态：${stateNames[trace.batch.state]||'状态待核对'}`,items};
}
