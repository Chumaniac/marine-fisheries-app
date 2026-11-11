const paths=new Set(['/v1/environment-readings','/v1/catches','/v1/batches']);
const id=value=>typeof value==='string'&&/^[A-Za-z0-9_-]{8,128}$/.test(value);
const hash=value=>typeof value==='string'&&/^[a-f0-9]{64}$/.test(value);
const scopeKey=value=>value?.scopeHash&&value.targetId&&value.editable&&value.online&&value.role==='operator'?JSON.stringify([value.scopeHash,value.tenant,value.subject,value.organization,value.targetId,value.siteId,value.deviceId,value.startAt,value.endAt]):'';
export function createReceiptReuse({getScope,readSource,sendLink,onState}){
 let key='',epoch=0,controller=null,source=null,intent=null,disposed=false,state='idle',message='读取另一份原工单，再选择已经收件的记录。';
 function emit(){if(!disposed)onState({state,message,available:Boolean(key),source});}
 function reset(){epoch++;controller?.abort();controller=null;source=null;intent=null;state='idle';message='读取另一份原工单，再选择已经收件的记录。';}
 function contextChanged(){const next=scopeKey(getScope());if(next!==key){reset();key=next;emit();}return key;}
 function resetSource(){reset();key=scopeKey(getScope());emit();}
 async function load(sourceId){
  if(disposed)return;contextChanged();reset();key=scopeKey(getScope());const scope=getScope(),request=epoch,bound=key;
  if(!bound||!id(sourceId)||sourceId===scope.targetId){state='error';message='请选择另一份可读取的原工单，并保持目标工单可录入且在线。';emit();return;}
  const active=new AbortController();controller=active;state='loading';message='正在读取原工单的收件元数据…';emit();
  try{const value=await readSource(sourceId,active.signal);if(disposed||request!==epoch||scopeKey(getScope())!==bound)return;
   if(!value||value.schema!=='marine.collection-records/v1'||value.id!==sourceId||value.tenant!==scope.tenant||value.siteId!==scope.siteId||!Number.isSafeInteger(value.revision)||value.revision<1||!Array.isArray(value.sources)||value.sources.length>100||!Array.isArray(value.records)||value.records.length>128||value.records.some(row=>!row||!id(row.requestId)||typeof row.recordId!=='string'||!row.recordId||row.recordId.length>128||!paths.has(row.path)||!hash(row.evidenceHash)))throw Error('原收件元数据、身份或站点不完整，请重新核对。');
   source={id:value.id,revision:value.revision,deviceId:value.deviceId,startAt:value.startAt,endAt:value.endAt,sourceCount:value.sources.length,records:value.records.map(row=>({requestId:row.requestId,recordId:row.recordId,path:row.path,kind:row.kind,state:row.state,evidenceHash:row.evidenceHash}))};state='ready';message=source.records.length?'已读取原收件。关联只写入当前工单，不重新登记、识别或采集。':'原工单没有已保存收件。';emit();
  }catch(error){if(!disposed&&request===epoch&&scopeKey(getScope())===bound){state='error';message=error.message;emit();}}
  finally{if(controller===active)controller=null;}
 }
 async function reuse(requestId,includeSources){
  contextChanged();const scope=getScope(),bound=key,request=epoch,row=source?.records.find(row=>row.requestId===requestId);
  if(disposed||!bound||!source||!row||row.state!=='received'||state==='loading'||state==='linking')throw Error('请先读取原工单并选择已完成收件的记录。');
  const existing=(scope.records||[]).find(saved=>saved.requestId===requestId);
  if(existing){
   if(existing.evidenceHash!==row.evidenceHash)throw Error('当前工单已有不同收件摘要，请先核对原记录。');
   state='received';message='已从当前工单核对到这份收件，无需重复关联，验收另行完成。';intent=null;emit();return {alreadyLinked:true};
  }
  const signature=JSON.stringify([bound,source.id,row.requestId,row.recordId,row.path,row.evidenceHash,Boolean(includeSources)]);
  if(intent?.signature!==signature)intent={signature,requestId:crypto.randomUUID()};
  const body={requestId:intent.requestId,expectedRevision:scope.revision,sourceOrderId:source.id,sourceRequestId:row.requestId,path:row.path,recordId:row.recordId,evidenceHash:row.evidenceHash,includeSources:Boolean(includeSources)};
  state='linking';message='正在关联已有收件；业务原件不会重做。';emit();
  try{const result=await sendLink(scope.targetId,body);if(disposed||request!==epoch||scopeKey(getScope())!==bound)return;
   if(result?.order?.id!==scope.targetId||result.order.tenant!==scope.tenant||result.syncReceipt?.requestId!==row.requestId||result.syncReceipt.evidenceHash!==row.evidenceHash||result.syncReceipt.associationRequestId!==body.requestId)throw Error('关联结果未确认，请刷新当前工单核对，原收件保持。');
   state='received';message='已有收件已关联；原工单和业务回执保持，验收另行完成。';intent=null;emit();return result;
  }catch(error){if(!disposed&&request===epoch&&scopeKey(getScope())===bound){state='error';message='关联结果未确认。可先刷新目标工单，或保留同一关联编号重试；不重做业务。';emit();throw error;}}
 }
 return {contextChanged,resetSource,load,reuse,dispose(){disposed=true;reset();}};
}
