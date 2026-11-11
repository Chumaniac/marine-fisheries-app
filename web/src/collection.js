import {validQueueActor} from './network.js';

export const COLLECTION_LIMITS={operations:8,sources:100,records:128};
export function ownedCollection(operation,actor){return Boolean(operation&&validQueueActor(actor)&&['scopeHash','tenant','subject'].every(k=>operation[k]===actor[k]));}
export function sourceReference(event){return {eventId:event.eventId,eventType:event.eventType,siteId:event.siteId,deviceId:event.deviceId,occurredAt:event.occurredAt,revision:event.revision??null};}
// Persisted JSON may order keys differently; compare the immutable source fields.
function sameSourceReference(left,right){
 return left.eventId===right.eventId&&left.eventType===right.eventType&&left.siteId===right.siteId&&
  left.deviceId===right.deviceId&&left.occurredAt===right.occurredAt&&(left.revision??null)===(right.revision??null);
}
export function checkSource(operation,event){
 const at=Date.parse(event?.occurredAt)/1000;
 if(!event||!['fish.observation','environment.reading'].includes(event.eventType)||event.siteId!==operation.siteId||!Number.isFinite(at)||at<operation.startAt||at>operation.endAt)throw new Error('来源不属于本站点或作业时间范围，请重新选择。');
}
export function createCollection(actor,{id,siteId,deviceId,startAt,endAt},devices){
 if(!validQueueActor(actor))throw new Error('请先连接有效会话');
 if(!Number.isFinite(startAt)||!Number.isFinite(endAt)||startAt>endAt||endAt-startAt>7*86400)throw new Error('请选择有效作业时间范围，最多7天。');
 const device=devices.find(d=>d.deviceId===deviceId);
 if(!device?.enabled||device.siteId!==siteId)throw new Error('请选择本站点已启用的设备。');
 return {schema:'marine.collection/v1',id,scopeHash:actor.scopeHash,tenant:actor.tenant,subject:actor.subject,siteId,deviceId,startAt,endAt,revision:1,sources:[],records:[],closed:false};
}
export function attachCollectionSources(operation,events){
 const sources=new Map(operation.sources.map(e=>[e.eventId,e]));
 for(const event of events){checkSource(operation,event);const ref=sourceReference(event),previous=sources.get(ref.eventId);if(previous&&!sameSourceReference(previous,ref))throw new Error('来源版本或元数据已变化，请核对原记录。');sources.set(ref.eventId,ref);}
 if(sources.size>COLLECTION_LIMITS.sources)throw new Error('一次作业最多关联100条来源，已有结果保留。');
 return {...operation,revision:operation.revision+1,sources:[...sources.values()]};
}
export function guardCollection(operation,actor,path,body,devices,events){
 if(operation.readOnly||operation.serverPersisted&&operation.state!=='open')throw new Error('当前工单只读或正在交接，请先选择可录入的工单。');
 if(!ownedCollection(operation,actor))throw new Error('作业身份已失效，请重新连接。');
 if(operation.records.length>=COLLECTION_LIMITS.records)throw new Error('作业记录已满，请保留本次作业并结束后新建。');
 if(devices.length&&!devices.some(d=>d.deviceId===operation.deviceId&&d.siteId===operation.siteId&&d.enabled))throw new Error('作业设备已停用或所属站点变化，请停止后续录入。');
 const at=path==='/v1/environment-readings'?body.occurredAt:path==='/v1/catches'?body.originAt:null;
 if(at!==null&&(!Number.isFinite(at)||at<operation.startAt||at>operation.endAt))throw new Error('录入时间不在当前作业范围内。');
 if(path==='/v1/environment-readings'&&body.deviceId!==operation.deviceId)throw new Error('录入设备与当前作业不一致。');
 if(path==='/v1/catches'){
  if(!(body.originAt<body.labelAvailableAt&&body.labelAvailableAt<=Date.now()/1000))throw new Error('确认时间须晚于作业起点，且不晚于现在。');
  if(body.siteId!==operation.siteId)throw new Error('渔获站点与当前作业不一致。');
  for(const id of body.sourceEventIds){const source=operation.sources.find(e=>e.eventId===id);if(!source)throw new Error('请先将所选来源关联到本次作业。');checkSource(operation,source);const current=events.find(e=>e.eventId===id);if(current&&!sameSourceReference(current,source))throw new Error('来源版本变化，请重新核对。');}
 }
 if(path==='/v1/batches'&&body.areaCode!==operation.siteId)throw new Error('批次区域与作业站点不同，请核对批次关联。');
}
export function collectionResult(item,result){
 const batch=result.batch||{},status=result.status;
 return {requestId:item.requestId,path:item.path,recordId:result.batchId||batch.batchId||result.labelId||result.sampleId||result.evidenceId||result.acceptedEventIds?.[0]||item.body?.batchId||item.requestId,
  revision:result.revision??batch.revision??null,state:['pending_review','review_pending','needs_review','review'].includes(status)?'review':'received',
  chainStatus:batch.chainStatus||result.chainStatus||null};
}
export function collectionTimeline(operation,pending){
 const rows=new Map(operation.records.map(r=>[r.requestId,r]));
 for(const item of pending.filter(item=>item.collectionId===operation.id))rows.set(item.requestId,{requestId:item.requestId,path:item.path,recordId:item.body?.batchId||item.requestId,state:item.blocked?'conflict':item.deliveryResult?'binding':'pending'});
 return [...rows.values()].slice(0,COLLECTION_LIMITS.records);
}
