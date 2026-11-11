import {MAX_DELIVERY_ATTEMPTS,scopedItems,validQueueActor,validDeliveryMetadata,deliveryDependencies,dependencyBarriers} from './network.js';

export const MAX_DIAGNOSTIC_INPUT=1000;
const MAX_DIAGNOSTIC_DETAILS=32;
function failureClass(item){
 if(!validDeliveryMetadata(item))return 'invalid-metadata';
 if(item.errorCode==='RETRY_BUDGET_EXHAUSTED')return 'retry-budget-exhausted';
 if(item.errorCode==='RETRY_WINDOW_EXPIRED')return 'window-expired';
 const status=item.lastStatus;
 if(status===401||status===403)return 'identity-required';
 if(status===409)return 'conflict';
 if([408,429,502,503,504].includes(status))return 'retryable';
 if(Number.isInteger(status)&&status>=400&&status<500)return 'rejected';
 if(Number.isInteger(status)&&status>=500&&status<600)return 'server-error';
 return 'not-recorded';
}

const operations=[[/^\/v1\/collection-orders$/,'创建采集工单'],[/^\/v1\/edge-samples$/,'登记图像观测'],[/^\/v1\/batches$/,'登记捕捞批次'],[/\/evidence$/,'上传批次证据'],[/\/transitions$/,'更新批次状态'],[/\/transfers$/,'申请机构交接'],[/\/accept$/,'确认机构交接'],[/\/transform/,'登记批次加工'],[/\/cold/,'登记冷链记录'],[/^\/v1\/environment-readings$/,'录入环境观测'],[/^\/v1\/catches$/,'登记渔获记录']];
export function operationLabel(path){return operations.find(([pattern])=>pattern.test(path))?.[1]||'待同步操作';}
export function queueOverview(items,actor,now=Date.now()) {
 const blocked=new Set(),counts={all:0,manual:0,waiting:0,ready:0};
 const rows=scopedItems(items,actor,now).map(item=>{
  const dependencies=deliveryDependencies(item);
  const preceding=dependencies.some(key=>blocked.has(key));
  const state=item.blocked||!validDeliveryMetadata(item)?'manual':preceding||item.next>now?'waiting':'ready';
  if(state!=='ready')for(const key of dependencyBarriers(item))blocked.add(key);
  const nextSeconds=Number.isFinite(item.next)?Math.max(0,Math.ceil((item.next-now)/1000)):0;
  let hint=state==='manual'?'自动重试已暂停。先核对原操作和服务端记录，再手动重试。':preceding?'等待同一批次的前序操作，请先处理前序记录。':nextSeconds?`等待下一次重试，约 ${nextSeconds} 秒。`:'连接恢复后将按顺序同步，请勿重复录入。';
  if(state==='manual'&&item.lastStatus===401)hint='请重新登录当前账户，再核对并重试原记录。';
  if(state==='manual'&&item.lastStatus===403)hint='请核对当前机构的操作权限，联系管理员确认后再重试。';
  if(state==='manual'&&item.lastStatus===409)hint='请查询批次最新状态。需要更新版本时，选择“核对最新状态并重建操作”。';
  if(state==='manual'&&item.lastStatus===413)hint='文件超过允许大小，请保留原件，在证据上传表单中重新核对。';
  if(!validDeliveryMetadata(item))hint='本机保存的同步信息不完整，原操作已保留；请先人工核对，不自动重试。';
  if(item.deliveryResult)hint='服务端已收件，重试只补齐工单关联或加载，不重新提交业务。'+(state==='manual'?' 请刷新工单状态后重试关联。':'');
  counts.all++;counts[state]++;
  return {requestId:item.requestId,label:operationLabel(item.path||'/v1/batches'),state,attempts:Math.min(MAX_DELIVERY_ATTEMPTS,Math.max(0,Number.isFinite(item.attempt)?item.attempt:0)),attemptsKnown:Number.isSafeInteger(item.attempt??0)&&(item.attempt??0)>=0&&(item.attempt??0)<=MAX_DELIVERY_ATTEMPTS,maximumAttempts:MAX_DELIVERY_ATTEMPTS,failureClass:failureClass(item),hint};
 });
 return {rows,counts};
}

export function queueDiagnostics(items,actor,now=Date.now()){
 if(!validQueueActor(actor,now))throw new Error('请先连接有效会话，无法导出其他账户或过期身份的诊断。');
 if(!Array.isArray(items)||items.length>MAX_DIAGNOSTIC_INPUT)throw new Error('本机队列超出诊断容量，原记录仍保留。');
 const date=new Date(now);if(!Number.isFinite(date.getTime()))throw new Error('本机时间无效，未生成诊断。');
 let overview;
 try{overview=queueOverview(items,actor,now);}catch{throw new Error('本机队列结构异常，无法导出完整诊断。原记录仍保留。');}
 const classes=Object.fromEntries(['invalid-metadata','retry-budget-exhausted','window-expired','identity-required','conflict','retryable','rejected','server-error','not-recorded'].map(code=>[code,0]));
 for(const row of overview.rows)classes[row.failureClass]++;
 const report={schema:'marine.queue-diagnostics/v1',source:'local-queue-snapshot',observedAt:date.toISOString(),counts:overview.counts,failureClasses:classes,
  details:overview.rows.slice(0,MAX_DIAGNOSTIC_DETAILS).map((row,index)=>({position:index+1,operation:row.label,state:row.state,attempts:row.attemptsKnown?row.attempts:null,failureClass:row.failureClass})),
  detailsOmitted:Math.max(0,overview.rows.length-MAX_DIAGNOSTIC_DETAILS),evidence:'diagnostic-only-not-delivery-proof'};
 if(new TextEncoder().encode(JSON.stringify(report,null,2)).length>16*1024)throw new Error('诊断超过容量限制，原记录仍保留。');
 return report;
}
