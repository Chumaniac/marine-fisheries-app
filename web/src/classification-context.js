/** One ownership snapshot; the original File/result remain shared and unchanged. */
export function classificationOwner(context){
 return {scopeHash:context?.scopeHash||null,orderId:context?.orderId??null,revision:context?.revision??null,siteId:context?.siteId??null,deviceId:context?.deviceId??null};
}
export function classificationRegistration(capture,context){
 if(!capture)return {state:'empty',canRecord:false,canReuse:false};
 const owner=capture.owner,current=classificationOwner(context);
 if(!owner?.scopeHash||owner.scopeHash!==current.scopeHash)return {state:'foreign',canRecord:false,canReuse:false};
 if(capture.recorded)return {state:'recorded',canRecord:false,canReuse:false};
 // An attempted event is immutable. A newer revision can read/retry that same
 // event in its original order, but cannot rebind its site/device/observation.
 const same=['orderId','siteId','deviceId',...(capture.event?[]:['revision'])].every(key=>owner[key]===current[key]);
 return {state:same?(capture.event?'pending':'ready'):'changed',canRecord:same&&Boolean(current.deviceId),canReuse:!same&&!capture.event&&Boolean(current.deviceId)};
}
