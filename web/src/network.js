export const MAX_DELIVERY_ATTEMPTS=8;
export const DELIVERY_WINDOW_MS=86400000;
export function retryable(status) {
  return !status || [408,429,502,503,504].includes(status);
}
export function retryDelay(attempt,retryAfter=0,random=Math.random) {
  return Math.max(Math.max(0,retryAfter)*1000,random()*Math.min(300000,2000*2**Math.min(Math.max(0,attempt),8)));
}
export function usableSnapshot(snapshot,now=Date.now()) {
  return Boolean(snapshot&&now>=snapshot.saved&&now-snapshot.saved<86400000);
}
export function validQueueActor(actor,now=Date.now()) {
  return Boolean(actor&&Number.isFinite(now)&&Number.isFinite(actor.expiresAt)&&
    Number.isFinite(actor.expiresAt*1000)&&actor.expiresAt*1000>now&&
    ['scopeHash','tenant','subject'].every(key=>typeof actor[key]==='string'&&actor[key].length>0));
}
export function scopedItems(items,actor,now=Date.now()) {
  if(!validQueueActor(actor,now))return [];
  return items.filter(item=>item&&item.scopeHash===actor.scopeHash&&item.tenant===actor.tenant&&item.subject===actor.subject).sort((a,b)=>(a.createdAt||0)-(b.createdAt||0));
}
export function validDeliveryMetadata(item){
 const attempt=item.attempt??0;
 return Number.isSafeInteger(attempt)&&attempt>=0&&attempt<=MAX_DELIVERY_ATTEMPTS&&
   ['next','createdAt','retryStartedAt'].every(key=>item[key]==null||Number.isFinite(item[key]))&&
   (item.blocked==null||typeof item.blocked==='boolean')&&
   (item.dependencies==null||(Array.isArray(item.dependencies)&&item.dependencies.every(key=>typeof key==='string'&&key.length>0)));
}
export function deliveryDependencies(item){
 if(!Array.isArray(item.dependencies)||!item.dependencies.every(key=>typeof key==='string'&&key.length>0))
  return [typeof item.body?.batchId==='string'&&item.body.batchId?item.body.batchId:'legacy-ordered'];
 // Older persisted orders also recorded a generic endpoint key. Keep their exact stored
 // request and evidence, but isolate unrelated orders; explicit batch dependencies remain ordered.
 const orderKey='collection:'+item.collectionId;
 if(item.collectionServer===true&&typeof item.collectionId==='string'&&item.collectionId&&
    ['/v1/environment-readings','/v1/catches'].includes(item.path)&&!item.batchId&&!item.body?.batchId&&
    item.dependencies.includes(orderKey))return item.dependencies.filter(key=>key!==item.path);
 return item.dependencies;
}
export function dependencyBarriers(item){
 const effective=deliveryDependencies(item);
 // Preserve generic legacy barriers for records whose binding cannot be established.
 return Array.isArray(item.dependencies)?[...new Set([...effective,...item.dependencies.filter(key=>typeof key==='string'&&key.length>0)])]:effective;
}
export async function deliverPending(items,{send,remove,save,now=Date.now(),random=Math.random}) {
  const blocked=new Set();
  for (const item of items) {
    const dependencies=deliveryDependencies(item);
    if(item.blocked||dependencies.some(key=>blocked.has(key))){for(const key of dependencyBarriers(item))blocked.add(key);continue;}
    if(!validDeliveryMetadata(item)){
      // Preserve request and failure history; unknown counters must not bypass retry bounds.
      await save({...item,blocked:true});for(const key of dependencyBarriers(item))blocked.add(key);continue;
    }
    const attempt=item.attempt||0,start=item.retryStartedAt??item.createdAt;
    const exhausted=attempt>=MAX_DELIVERY_ATTEMPTS;
    const expired=Number.isFinite(start)&&(now<start||now-start>=DELIVERY_WINDOW_MS);
    if(exhausted||expired){await save({...item,blocked:true,errorCode:exhausted?'RETRY_BUDGET_EXHAUSTED':'RETRY_WINDOW_EXPIRED',lastMessage:'自动重试已停止，请核对原请求后手动重试。'});for(const key of dependencyBarriers(item))blocked.add(key);continue;}
    if(item.next>now){for(const key of dependencyBarriers(item))blocked.add(key);continue;}
    try {await send(item.body,item);await remove(item.requestId);}
    catch(error) {
      // Preserve the original request and its idempotency key after uncertain delivery.
      await save({...item,blocked:!retryable(error.status)||attempt+1>=MAX_DELIVERY_ATTEMPTS,lastStatus:error.status||0,errorCode:attempt+1>=MAX_DELIVERY_ATTEMPTS?'RETRY_BUDGET_EXHAUSTED':error.code||null,lastMessage:attempt+1>=MAX_DELIVERY_ATTEMPTS?'自动重试已停止，请核对原请求后手动重试。':String(error.message||'').slice(0,200),attempt:attempt+1,next:now+retryDelay(attempt+1,error.retryAfter,random)});
      for(const key of dependencyBarriers(item))blocked.add(key);
    }
  }
}


export function responseErrorMessage(status,body){
 const detail=body?.detail;
 if(Array.isArray(detail)&&detail.length){
  const fields={massGrams:'重量（克）',lossGrams:'损耗（克）',batchId:'批次编号',siteId:'站点',deviceId:'设备',speciesId:'物种',expectedRevision:'记录版本',requestId:'请求编号',temperatureC:'温度',salinityPSU:'盐度',turbidityNTU:'浊度',observedAt:'采集时间',startAt:'开始时间',endAt:'结束时间',minimumC:'最低温度',maximumC:'最高温度'};
  const problems=detail.slice(0,3).map(item=>{
   const key=Array.isArray(item?.loc)?item.loc.filter(value=>typeof value==='string'&&Object.hasOwn(fields,value)).at(-1):null;
   const type=typeof item?.type==='string'?item.type:'';
   const hint=type==='missing'?'不能为空':type.startsWith('int')?'需为整数':type.startsWith('float')||type==='finite_number'?'需为有效数字':['greater_than','less_than','greater_than_equal','less_than_equal'].includes(type)?'超出允许范围':'格式不正确';
   return (fields[key]||'提交内容')+hint;
  });
  return '请检查提交内容：'+[...new Set(problems)].join('；')+(detail.length>3?'；并核对其他字段。':'。');
 }
 const known={'Classifier model is not installed':'识别模型尚未安装，请联系管理员准备模型。','IPFS not configured; local original retained':'存储服务尚未配置，本机原件已保留。','IPFS unavailable; local original retained':'存储服务暂不可用，本机原件已保留。','Fabric not configured':'链服务尚未配置，请核对连接设置。','Batch commands must be reconciled before anchoring':'请先同步并核对批次流转，再提交存证。','Commit unconfirmed; retry reconciles existing receipt':'存证结果尚未确认，请先核对原回执。','Pin and verify evidence before anchoring':'请先固定并核验文件副本，再提交存证。','Session scope changed':'会话已切换，请重新连接。',INVALID_RESPONSE:'服务响应无法读取，请先查询当前状态。'};
 const code=typeof body?.code==='string'?body.code:typeof detail==='string'?detail:'';
 if(Object.hasOwn(known,code))return known[code];
 if(status===401)return '会话已失效，请重新连接。';
 if(status===403)return '当前账户没有此操作权限，请核对身份。';
 if(status===409)return '记录或请求状态已变化，请先查询最新状态。';
 return code?code.slice(0,180):`请求未完成（${status}），请检查输入或服务状态。`;
}
