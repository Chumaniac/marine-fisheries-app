import {qualityIssues} from './data-quality.js';

export const acceptanceIssues={...qualityIssues,collectionNotEnded:'采集尚未结束',sourcesMissing:'尚未关联来源',receiptsMissing:'尚无收件回执',deviceInvalid:'站点或设备授权已失效',sourceUnavailable:'来源不可读取',sourceBindingChanged:'来源版本或范围已变化',recordSourceUnlinked:'收件来源尚未关联',recordTypeUnsupported:'记录类型暂不支持',receiptUnavailable:'收件回执不可读取',receiptBindingChanged:'收件原件或范围已变化',recordNeedsReview:'业务记录仍需复核',batchRecalled:'批次已召回',coldIssueOpen:'批次仍有未关闭的冷链异常',batchEvidenceRequired:'所选等级需要关联批次证据',evidenceLevelMissing:'尚无达到所选等级的已有证据回执',evidenceInvalid:'证据元数据无效',ledgerUnconfirmed:'仍缺少已有账本确认回执',capacityExceeded:'达到本次检查容量，未作完整结论',documentTooLarge:'引用超过单项容量',documentInvalid:'引用内容格式无效'};
export const evidencePolicies={local:'本机来源与收件回执',pinned:'每批次至少一份已有固定证据',committed:'每批次已有账本确认及至少一份确认的证据'};
const id=value=>typeof value==='string'&&/^assessment_[a-f0-9]{32}$/.test(value);
const sha=value=>typeof value==='string'&&/^[a-f0-9]{64}$/.test(value);
const integer=(value,max)=>Number.isSafeInteger(value)&&value>=0&&value<=max;
const scopeKey=scope=>scope?.scopeHash&&scope?.orderId?JSON.stringify([scope.scopeHash,scope.orderId,scope.orderRevision,scope.online]):'';
export function validAssessment(value,scope){
 if(!value||value.schema!=='marine.collection-assessment/v1'||value.orderId!==scope.orderId||!integer(value.orderRevision,Number.MAX_SAFE_INTEGER)||!['ready','blocked'].includes(value.outcome)||!Object.hasOwn(evidencePolicies,value.evidencePolicy)||!sha(value.bindingSha256))return false;
 if(!value.coverage||!integer(value.coverage.sources,100)||!integer(value.coverage.records,128)||!integer(value.coverage.batches,128)||!integer(value.coverage.selectedPayloadBytes,2097152)||typeof value.coverage.complete!=='boolean'||value.coverage.orderReferencesOnly!==true||value.coverage.physicalImagesChecked!==false||value.coverage.remoteAvailabilityChecked!==false)return false;
 if(!value.issues||Object.entries(value.issues).some(([code,count])=>!Object.hasOwn(acceptanceIssues,code)||!integer(count,10000)||!count))return false;
 const total=Object.values(value.issues).reduce((sum,count)=>sum+count,0);
 if(!Array.isArray(value.findings)||value.findings.length!==Math.min(32,total)||value.findingsTruncated!==(total>32)||value.findings.some(row=>!row||typeof row.reference!=='string'||row.reference.length>200||!Object.hasOwn(acceptanceIssues,row.code)||!value.issues[row.code]))return false;
 const ready=value.coverage.complete&&Object.keys(value.issues).length===0;
 return value.outcome===(ready?'ready':'blocked')&&value.limits?.sources===100&&value.limits?.records===128&&value.limits?.batches===32&&value.limits?.evidencePerBatch===16&&value.limits?.inputBytes===2097152&&value.limits?.findings===32&&value.limits?.assessmentsPerOrder===8;
}
function validEnvelope(value,scope){
 return value&&validAssessment(value.assessment,scope)&&id(value.assessment.id)&&integer(value.version,2)&&value.version>=1&&value.current&&typeof value.current.stale==='boolean'&&sha(value.current.bindingSha256)&&['ready','blocked'].includes(value.current.outcome)&&
  (value.decision===null||value.decision?.assessmentId===value.assessment.id&&['approve','reject'].includes(value.decision.decision));
}
function canonical(value){let nodes=0;function encode(item,depth){if(++nodes>10000||depth>64)throw Error('验收凭证结构超过容量。');const text=Array.isArray(item)?'['+item.map(child=>encode(child,depth+1)).join(',')+']':item&&typeof item==='object'?'{'+Object.keys(item).sort().map(key=>JSON.stringify(key)+':'+encode(item[key],depth+1)).join(',')+'}':JSON.stringify(item);if(typeof text!=='string'||text.length>256*1024)throw Error('验收凭证超过容量。');return text;}return encode(value,0);}
export async function verifyCertificate(packet,scope,assessmentId){
 if(!packet||packet.schema!=='marine.collection-certificate/v1'||!sha(packet.sha256)||!validAssessment(packet.certificate?.assessment,scope)||packet.certificate.assessment.id!==assessmentId||packet.certificate.decision?.decision!=='approve'||packet.certificate.decision.assessmentId!==assessmentId||typeof packet.current?.stale!=='boolean')throw Error('验收凭证结构未确认。');
 const bytes=new TextEncoder().encode(canonical(packet.certificate));if(bytes.length>256*1024)throw Error('验收凭证超过容量。');
 const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),byte=>byte.toString(16).padStart(2,'0')).join('');
 if(hash!==packet.sha256)throw Error('验收凭证完整性不符。');
 return packet;
}

/** One selected, bounded review; writes require an explicit action and retain a retry key. */
export function createAcceptanceReview({getScope,request,onState}){
 let epoch=0,key='',controller=null,busy=false,disposed=false,pending=null;
 let view={preview:null,items:[],selected:null};
 function publish(state='idle',message=''){if(!disposed)onState({state,message,...view});}
 function reset(){epoch++;controller?.abort();controller=null;busy=false;view={preview:null,items:[],selected:null};key=scopeKey(getScope());pending=null;publish();}
 function contextChanged(){if(key!==scopeKey(getScope()))reset();}
 async function run(work){
  contextChanged();if(busy||disposed)return null;
  const scope=getScope();if(!key||!scope.online){publish('error','请联网并选择当前账户可访问的服务器工单。');return null;}
  const generation=epoch,expected=key;controller=new AbortController();const signal=controller.signal;busy=true;let failure='';publish('busy');
  try{
   const result=await work(scope,signal);
   if(disposed||epoch!==generation||scopeKey(getScope())!==expected)return null;
   if(result&&'certificate'in result){publish('ready');return result.certificate;}
   view={...view,...result};publish('ready');return result;
  }catch(error){failure=error.message||'验收操作未确认；写入失败后先刷新记录或使用同一请求重试。';if(!disposed&&epoch===generation&&scopeKey(getScope())===expected)publish('error',failure);return null;}
  finally{if(epoch===generation){busy=false;controller=null;if(!disposed&&scopeKey(getScope())===expected)publish(failure?'error':'ready',failure);}}
 }
 const path=scope=>'/v1/collection-orders/'+encodeURIComponent(scope.orderId);
 function selectedPatch(response){const a=response.assessment,item={id:a.id,orderRevision:a.orderRevision,createdBy:a.createdBy,createdAt:a.createdAt,outcome:a.outcome,evidencePolicy:a.evidencePolicy,version:response.version,decision:response.decision?.decision||null};const found=view.items.some(row=>row.id===item.id);const items=found?view.items.map(row=>row.id===item.id?item:row):[item,...view.items].slice(0,8);return {selected:response,preview:null,items};}
 async function command(scope,signal,route,body){
  const signature=JSON.stringify([key,route,body]);if(!pending||pending.signature!==signature)pending={signature,requestId:crypto.randomUUID()};
  const response=await request(route,{method:'POST',body:JSON.stringify({...body,requestId:pending.requestId}),signal,scopeHash:scope.scopeHash});
  if(!validEnvelope(response,scope))throw Error('验收响应不完整，请先刷新验收记录。');pending=null;return selectedPatch(response);
 }
 return {reset,contextChanged,dispose(){disposed=true;epoch++;controller?.abort();view={preview:null,items:[],selected:null};},
  preview(policy){return run(async(scope,signal)=>{const value=await request(path(scope)+'/acceptance-preview?'+new URLSearchParams({evidencePolicy:policy}),{signal,scopeHash:scope.scopeHash});if(!validAssessment(value,scope)||value.orderRevision!==scope.orderRevision)throw Error('检查响应或工单版本变化，请刷新工单。');return {preview:value,selected:null};});},
  list(){return run(async(scope,signal)=>{const value=await request(path(scope)+'/acceptance-assessments',{signal,scopeHash:scope.scopeHash});if(!value||value.orderRevision!==scope.orderRevision||!Array.isArray(value.items)||value.items.length>8||value.items.some(row=>!id(row.id)))throw Error('验收目录未确认，请刷新工单。');return {items:value.items};});},
  open(assessmentId){return run(async(scope,signal)=>{if(!id(assessmentId))throw Error('请选择验收记录。');const value=await request(path(scope)+'/acceptance-assessments/'+assessmentId,{signal,scopeHash:scope.scopeHash});if(!validEnvelope(value,scope))throw Error('验收记录结构未确认。');return selectedPatch(value);});},
  submit(policy){return run((scope,signal)=>command(scope,signal,path(scope)+'/acceptance-assessments',{expectedRevision:scope.orderRevision,evidencePolicy:policy}));},
  decide(decision,reason){return run((scope,signal)=>{const selected=view.selected;if(!selected||selected.current.stale||selected.decision)throw Error('请打开当前有效且未决定的验收记录。');return command(scope,signal,path(scope)+'/acceptance-assessments/'+selected.assessment.id+'/decisions',{expectedRevision:scope.orderRevision,expectedAssessmentVersion:selected.version,evidencePolicy:selected.assessment.evidencePolicy,decision,reason});});},
  certificate(){return run(async(scope,signal)=>{const selected=view.selected;if(!selected||selected.decision?.decision!=='approve')throw Error('请先打开独立验收通过的记录。');const packet=await request(path(scope)+'/acceptance-assessments/'+selected.assessment.id+'/certificate',{signal,scopeHash:scope.scopeHash});return {certificate:await verifyCertificate(packet,scope,selected.assessment.id)};});},
 };
}
