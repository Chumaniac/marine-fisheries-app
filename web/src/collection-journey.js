import {scopedItems,validQueueActor} from './network.js';
import {queueOverview} from './queue-view.js';

/** Project already loaded evidence only; navigation never makes a business request. */
export function collectionJourney({order,actor,pending=[],acceptance=null,online=true,now=Date.now()}) {
 if(!order||!validQueueActor(actor,now)||!['scopeHash','tenant','subject'].every(key=>order[key]===actor[key]))return null;
 const queue=scopedItems(pending,actor,now).filter(item=>item.collectionId===order.id),overview=queueOverview(queue,actor,now);
 const counts={sources:order.sources.length,receipts:order.records.length,local:queue.filter(item=>!item.deliveryResult).length,linking:queue.filter(item=>item.deliveryResult).length,manual:overview.counts.manual};
 const result=(title,detail,label,target,requestId=null)=>({title,detail,label,target,counts,requestId});
 if(queue.length){const first=overview.rows.find(row=>row.state==='manual')||overview.rows[0];return result(online?'本机记录已保留，先核对同步':'本机已保存，待联网同步',`本机待同步 ${counts.local} 条；业务已收件、待关联 ${counts.linking} 条；需处理 ${counts.manual} 条。请查看原记录，不重复录入。`,'查看本工单同步详情','queue-list',first.requestId);}
 if(!online)return result('离线缓存 · 当前工单','当前显示本机保留的工单与回执；联网后核对最新状态再继续。','查看已存记录','collection-timeline');
 const offered=order.handoffs.find(item=>item.state==='pending');
 if(offered)return offered.recipient.subject===actor.subject?result('工单等待你接收','先核对来源与收件记录，再明确确认接收。','前往核对接收','collection-accept'):result('等待接收人确认','交接已提出，接收回执仍待对方确认。','查看交接记录','collection-handoffs');
 const selected=acceptance?.scopeHash===actor.scopeHash&&acceptance.selected?.assessment?.orderId===order.id?acceptance.selected:null,current=selected?.assessment?.orderId===order.id&&selected.assessment.orderRevision===order.revision&&!selected.current?.stale&&selected.current?.outcome==='ready';
 if(current&&selected.decision?.decision==='approve')return result('独立验收已通过','当前已加载的验收对应此工单版本；可查看凭证中的检查范围。','查看验收凭证','acceptance-certificate');
 if(order.state==='closed'||order.state==='handed-over'){
  if(selected?.current?.stale)return result('验收依据已变化，请重新核对','保留的历史凭证仍可查阅；当前版本需重新检查。','前往重新检查','acceptance-preview');
  if(selected?.decision?.decision==='reject')return result('验收已退回，先核对问题','查看退回说明与当前检查定位，处理后再提交。','查看验收说明','acceptance-state');
  if(current&&!selected.decision)return result('检查记录已保存，等待独立审核','检查记录与最终验收分别保留，请由另一名授权人员复核。','查看验收记录','acceptance-history');
  return result('采集已结束，接着核对验收','结束工单保留采集记录；请检查当前版本的来源、回执与业务状态。','前往验收检查','acceptance-preview');
 }
 if(order.readOnly)return result('以只读方式查看工单','当前身份可查看这份工单的已存记录。','查看来源与收件','collection-timeline');
 return order.records.length?result('回执已关联，核对后结束工单','先核对所关联业务的状态，准备好后再明确结束本次采集。','前往结束工单','collection-close'):result('先关联来源，继续录入','选择当前站点与时间范围内的来源，或读取另一份工单的已有收件。','选择采集来源','collection-sources');
}
