import {validQueueActor} from './network.js';

export function serverCollection(value,actor){
 if(!validQueueActor(actor)||!value||typeof value.id!=='string'||!value.id||!['open','closed','handoff-pending','handed-over'].includes(value.state)||!Number.isFinite(value.startAt)||!Number.isFinite(value.endAt)||typeof value.createdBy!=='string'||value.schema!=='marine.collection-order/v1'||value.tenant!==actor.tenant||!value.serverPersisted||!Number.isSafeInteger(value.revision)||value.revision<1||!Array.isArray(value.sources)||value.sources.length>100||!Array.isArray(value.records)||value.records.length>128||!Array.isArray(value.handoffs)||value.handoffs.length>20||value.sources.some(item=>!item||typeof item.eventId!=='string')||value.records.some(item=>!item||typeof item.requestId!=='string')||value.handoffs.some(item=>!item||typeof item.recipient?.subject!=='string'))throw new Error('服务器工单格式或身份需核对，原记录保留。');
 return {...value,subject:actor.subject,scopeHash:actor.scopeHash,readOnly:value.createdBy!==actor.subject||value.organization!==actor.organization||actor.role!=='operator'||value.state!=='open'};
}
export function receiptSummary(result){
 const summary={};
 for(const key of ['id','siteId','deviceId','startAt','endAt','serverPersisted','batchId','labelId','sampleId','evidenceId','transferId','proposalId','state','ownerMSP','massGrams','speciesId','areaCode','excursion','totalLedgerCommands','totalEvidence','openColdIssues','revision','status','chainStatus','count','cpue','unit']){
  const value=result?.[key];if(typeof value==='string'&&value.length<=512||typeof value==='number'&&Number.isFinite(value)||typeof value==='boolean')summary[key]=value;
 }
 if(Array.isArray(result?.acceptedEventIds))summary.acceptedEventIds=result.acceptedEventIds.filter(value=>typeof value==='string'&&value.length<=128).slice(0,128);
 if(Array.isArray(result?.ledgerCommands)&&summary.totalLedgerCommands==null)summary.totalLedgerCommands=result.ledgerCommands.length;
 if(Array.isArray(result?.evidence)&&summary.totalEvidence==null)summary.totalEvidence=result.evidence.length;
 if(result?.batch&&typeof result.batch==='object'){
  summary.batch={};for(const key of ['batchId','revision','status','state','chainStatus','organization','ownerMSP','massGrams','speciesId','localPending','openColdIssues','areaCode']){const value=result.batch[key];if(typeof value==='string'&&value.length<=512||typeof value==='number'&&Number.isFinite(value))summary.batch[key]=value;}
 }
 return summary;
}
async function intentDigest(item){
 const input=JSON.stringify({path:item.path,method:item.method||'POST',body:item.body??null,file:item.file?{size:item.file.size,type:item.file.type}:null});
 const bytes=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(input));
 return [...new Uint8Array(bytes)].map(value=>value.toString(16).padStart(2,'0')).join('');
}
export async function deliverKnownOnce(item,{send,persist,link}){
 const bodyDigest=await intentDigest(item);
 if(item.deliveryResult){
  const saved=item.deliveryResult;
  if(saved.schema!=='marine.queue-receipt/v1'||saved.requestId!==item.requestId||saved.bodyDigest!==bodyDigest||!saved.result||typeof saved.result!=='object'){
   const error=new Error('已收件凭据与当前请求不符，原操作保留，请先核对。');error.status=409;throw error;
  }
 }else{
  const result=await send();
  // Mutate this queued snapshot before persistence: retry metadata must retain a
  // known successful result even when association fails or storage reports an error.
  item.deliveryResult={schema:'marine.queue-receipt/v1',requestId:item.requestId,bodyDigest,result:receiptSummary(result)};
  await persist(item);
 }
 await link(item.deliveryResult.result);
 return item.deliveryResult.result;
}
