export const qualityIssues={schemaInvalid:'事件格式不符合契约',hashMismatch:'内容摘要与存储记录不一致',identityMismatch:'事件身份与存储记录不一致',environmentIncomplete:'环境指标有缺项',sensorSuspect:'传感器标记为待核对',sensorInvalid:'传感器标记为无效'};
const countKeys=['fishSelected','fishOtherSpecies','environment','other'];
const limits={records:500,bytes:1048576,recordBytes:8192,findings:32};
const integer=(value,max)=>Number.isSafeInteger(value)&&value>=0&&value<=max;
const uuid=value=>typeof value==='string'&&/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);

export function validQualityReport(value,scope){
 if(!value||value.schema!=='marine.data-quality/v1'||value.siteId!==scope.siteId||value.speciesId!==scope.speciesId)return false;
 if(!integer(value.checkedRecords,500)||!integer(value.checkedBytes,1048576)||!integer(value.findingRecords,value.checkedRecords))return false;
 if(typeof value.partial!=='boolean'||typeof value.findingsTruncated!=='boolean'||!value.coverage||!value.limits)return false;
 if(Object.entries(limits).some(([key,expected])=>value.limits[key]!==expected))return false;
 if(value.coverage.recognizedSiteOnly!==true||value.coverage.includesOtherSpecies!==true||value.coverage.unattributableRecordsExcluded!==true||value.coverage.physicalImagesChecked!==false)return false;
 if(!value.counts||countKeys.some(key=>!integer(value.counts[key],value.checkedRecords))||countKeys.reduce((sum,key)=>sum+value.counts[key],0)!==value.checkedRecords)return false;
 if(!value.issues||Object.keys(qualityIssues).some(key=>!integer(value.issues[key],value.findingRecords)))return false;
 const totalIssues=Object.keys(qualityIssues).reduce((sum,key)=>sum+value.issues[key],0);
 if(value.findingRecords>totalIssues||(!value.checkedRecords&&value.checkedBytes))return false;
 const expected=value.partial?'partial':value.findingRecords?'needs-review':value.checkedRecords?'checked':'empty';
 if(value.state!==expected||(value.partial?!['record-budget','byte-budget'].includes(value.partialReason):value.partialReason!==null))return false;
 if(!Array.isArray(value.findings)||value.findings.length!==Math.min(32,value.findingRecords)||value.findingsTruncated!==(value.findingRecords>32))return false;
 let previous=-1;
 return value.findings.every(row=>{
  if(!row||!integer(row.position,value.checkedRecords-1)||row.position<=previous||(row.eventId!==null&&!uuid(row.eventId)))return false;
  previous=row.position;
  return Array.isArray(row.issues)&&row.issues.length>0&&row.issues.length<=6&&new Set(row.issues).size===row.issues.length&&row.issues.every(code=>Object.hasOwn(qualityIssues,code)&&value.issues[code]>0);
 });
}

// One active request and one bounded report; reset aborts and invalidates late replies.
export function createQualityCheck({fetchReport,onState}){
 let epoch=0,controller=null,disposed=false;
 function reset(){epoch++;controller?.abort();controller=null;if(!disposed)onState({state:'idle'});}
 return {reset,dispose(){disposed=true;reset();},async run(scope){
  if(disposed)return;
  reset();
  if(!scope.scopeHash){onState({state:'error',message:'请先连接会话，再检查当前站点。'});return;}
  if(![scope.siteId,scope.speciesId].every(value=>typeof value==='string'&&value.length>0&&value.length<=128)){onState({state:'error',message:'请填写站点和物种后重试。'});return;}
  // Capture only this request's selectors; later input changes cannot relabel it.
  const selected=Object.freeze({siteId:scope.siteId,speciesId:scope.speciesId});
  const requestScope={...selected,scopeHash:scope.scopeHash};
  const request=epoch;controller=new AbortController();const signal=controller.signal;
  onState({state:'busy',scope:selected});
  try{
   const report=await fetchReport(requestScope,signal);
   if(disposed||request!==epoch)return;
   if(!validQualityReport(report,selected))throw new Error('INVALID_QUALITY_RESPONSE');
   onState({state:'ready',scope:selected,report});
  }catch(error){if(!disposed&&request===epoch)onState({state:'error',scope:selected,message:error.message==='INVALID_QUALITY_RESPONSE'?'检查响应不完整，请重新检查。':'检查未完成，请核对连接后重试。'});}
  finally{if(request===epoch)controller=null;}
 }};
}
