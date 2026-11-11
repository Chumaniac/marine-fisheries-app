const limits={events:500000,outbox_active:100000,labels:10000,batches:10000,reviews:1000,review_bytes:268435456,edge_admissions:1000,edge_evidence:1000};
const labels={events:'事件记录',outbox_active:'待处理投递',labels:'训练标签',batches:'批次记录',reviews:'人工复核样本',review_bytes:'复核图像字节',edge_admissions:'边缘受理记录',edge_evidence:'边缘证据记录'};
const stages={received:'已受理，后续未记录',review_pending:'待人工复核',published:'已发布阶段记录',review_rejected:'复核退回',unknown:'阶段待核对'};
const actions={check_original_request:'先核对设备原请求和现有结果，不直接重发',review_existing_sample:'到人工复核列表核对原样本，不重新识别',manual_check:'请管理员核对原请求与阶段记录'};
const reasons=['role_required','table_unavailable','query_unavailable','record_unavailable','scan_limit','inspection_busy'];
const count=v=>Number.isSafeInteger(v)&&v>=0;
const id=v=>typeof v==='string'&&v.length>0&&v.length<=512&&!/[\u0000-\u001f\u007f-\u009f]/.test(v);
const object=v=>v&&typeof v==='object'&&!Array.isArray(v);
export function validOperationalWorkload(v,tenant){
 if(!object(v)||v.schema!=='marine.operational-workload/v1'||v.tenant!==tenant||v.readOnly!==true||!['allowed','restricted'].includes(v.access)||!Number.isFinite(v.observedAt)||v.observedAt<=0||typeof v.complete!=='boolean'||!Array.isArray(v.capacity)||!object(v.admissions))return false;
 const a=v.admissions;
 if(typeof a.available!=='boolean'||!Array.isArray(a.items)||a.items.length>20||typeof a.truncated!=='boolean'||a.reason!==null&&!reasons.includes(a.reason))return false;
 if(v.access==='restricted')return !v.complete&&v.capacity.length===0&&!a.available&&a.counts===null&&a.items.length===0&&a.reason==='role_required';
 if(v.capacity.length!==8||new Set(v.capacity.map(r=>r?.key)).size!==8)return false;
 for(const row of v.capacity){
  if(!object(row)||!Object.hasOwn(limits,row.key)||row.limit!==limits[row.key]||row.unit!==(row.key==='review_bytes'?'bytes':'records'))return false;
  if(row.used===null){if(row.status!=='unknown'||!reasons.includes(row.reason))return false;}
  else{if(!count(row.used)||row.reason!==null||row.status!==(row.used>=row.limit?'full':row.used*5>=row.limit*4?'near':'ok'))return false;}
 }
 if(!a.available)return !v.complete&&a.counts===null&&a.items.length===0&&reasons.includes(a.reason);
 if(!object(a.counts)||Object.keys(a.counts).length!==5||!Object.keys(stages).every(key=>count(a.counts[key])))return false;
 const total=Object.values(a.counts).reduce((sum,n)=>sum+n,0);
 if(!count(total)||v.capacity.find(row=>row.key==='edge_admissions').used!==total||a.reason!==null&&a.reason!=='record_unavailable')return false;
 if(!a.items.every(row=>object(row)&&id(row.sampleId)&&id(row.deviceId)&&['received','review_pending','unknown'].includes(row.status)&&row.recordedAt===null&&row.cause==='not_recorded'&&row.nextAction===(row.status==='received'?'check_original_request':row.status==='review_pending'?'review_existing_sample':'manual_check')))return false;
 if(new Set(a.items.map(row=>row.sampleId)).size!==a.items.length)return false;
 if(['received','review_pending','unknown'].some(stage=>a.items.filter(row=>row.status===stage).length>a.counts[stage]))return false;
 return v.complete===(v.capacity.every(row=>row.status!=='unknown')&&!a.truncated&&a.counts.unknown===0);
}

// Read all three bounded legacy projections before replacing a visible snapshot.
export function validOperationsSummary(v){
 return object(v)&&typeof v.ready==='boolean'&&['pendingEvents','actionRequiredEvents','unconfirmedLedgerCommands'].every(k=>count(v[k]))&&Array.isArray(v.incidents)&&v.incidents.length<=100&&v.incidents.every(r=>object(r)&&id(r.id)&&id(r.code)&&['ACTIVE','ACKNOWLEDGED','RESOLVED'].includes(r.status));
}
export function validOutboxPage(v){
 return object(v)&&typeof v.hasMore==='boolean'&&count(v.nextCursor)&&Array.isArray(v.items)&&v.items.length<=100&&v.items.every(r=>object(r)&&id(r.id)&&count(r.attempt)&&['action_required','outcome_unknown','dispatching'].includes(r.status));
}
export function validLedgerStatus(v){
 return object(v)&&Array.isArray(v.jobs)&&v.jobs.length<=1000&&v.jobs.every(r=>object(r)&&id(r.id)&&id(r.batch)&&id(r.operation)&&['pending','submitted','committed'].includes(r.status));
}

/** Pure DOM projection with an explicit read ticket; no timers or business actions. */
export function createOperationsView({root,getScope}){
 const doc=root.ownerDocument;let serial=0;
 const node=(tag,text)=>{const e=doc.createElement(tag);if(text!==undefined)e.textContent=text;return e;};
 const key=()=>{const a=getScope();return a?[a.scopeHash,a.tenant,a.subject,a.role].join('|'):'';};
 const current=ticket=>ticket&&ticket.serial===serial&&ticket.scope===key()&&Boolean(ticket.scope);
 function clear(message='登录后按需检查受理阶段与存储预算。'){serial++;root.replaceChildren();root.dataset.state='empty';const p=node('p',message);p.setAttribute('role','status');root.append(p);}
 function begin(){clear('正在只读核对受理阶段与预算…');return {serial,scope:key()};}
 function fail(ticket){if(!current(ticket))return false;clear('受理与预算暂未核对，不能当作已处理或尚有容量。请检查连接后重试。');root.dataset.state='unavailable';return false;}
 function render(value,ticket){
  if(!current(ticket))return false;
  const actor=getScope();if(!validOperationalWorkload(value,actor.tenant)||value.access==='allowed'&&!['operator','auditor'].includes(actor.role))return fail(ticket);
  root.replaceChildren();root.dataset.state=value.access==='restricted'?'restricted':value.complete?'observed':'partial';
  root.append(node('h3','受理阶段与存储预算'));
  if(value.access==='restricted'){root.append(node('p','当前身份不能读取租户受理明细与预算，请由操作员或审计员核对。'));return true;}
  const table=node('table'),head=node('thead'),tr=node('tr');for(const text of ['项目','已用 / 预算','状态'])tr.append(node('th',text));head.append(tr);table.append(head);const body=node('tbody');
  const names={ok:'尚有预算',near:'接近预算（80%）',full:'已达到预算',unknown:'待核对'};
  const number=n=>new Intl.NumberFormat('zh-CN').format(n);
  for(const row of value.capacity){const item=node('tr'),heading=node('th',labels[row.key]);heading.setAttribute('scope','row');item.append(heading,node('td',(row.used===null?'未核对':number(row.used))+' / '+number(row.limit)+(row.unit==='bytes'?' 字节':' 条')),node('td',names[row.status]));body.append(item);}table.append(body);const wrap=node('div');wrap.className='operations-table';wrap.tabIndex=0;wrap.setAttribute('role','region');wrap.setAttribute('aria-label','当前租户预算');wrap.append(table);root.append(wrap);
  const a=value.admissions;
  if(!a.available)root.append(node('p','受理阶段暂不可读取，数量未知；没有自动重投或删除记录。'));
  else{
   root.append(node('h4','边缘受理阶段'));const list=node('ul');for(const stage of Object.keys(stages))list.append(node('li',stages[stage]+'：'+number(a.counts[stage])));root.append(list);
   if(a.items.length){const rows=node('ul');rows.className='admission-observations';for(const item of a.items){const li=node('li');li.append(node('strong',stages[item.status]),node('code',item.sampleId),node('p','设备：'+item.deviceId),node('p',actions[item.nextAction]));rows.append(li);}root.append(rows);}
   else root.append(node('p','本次未读取到待处理阶段记录，不代表所有现场请求均已完成。'));
   if(a.truncated)root.append(node('p','仅显示最多20条可核对示例，部分明细未显示。'));
   root.append(node('p','原记录没有可靠的受理时间或失败原因，当前均为未记录；不能据此判断停留时长或确认失败。'));
  }
  root.append(node('p','预算只观察当前租户记录，不自动清理旧数据；异常或未知结果先核对原请求与已有证据。'));
  return true;
 }
 clear();return {begin,render,fail,clear,current};
}
