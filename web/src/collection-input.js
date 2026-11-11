const forms=new Set(['collection-form','environment-form','catch-form','batch-form','sources']);
const copy=context=>context?{scopeHash:context.scopeHash,orderId:context.orderId??null,revision:context.revision??null,siteId:context.siteId??null,deviceId:context.deviceId??null}:null;
const key=context=>JSON.stringify(copy(context));
/** Only bounded ownership metadata; field values and files stay in their form nodes. */
export function createCollectionInputGuard(){
 let owner=null,sequence=0,review=false;const dirty=new Map();
 const state=context=>({dirty:dirty.size,forms:[...dirty.keys()],owner:copy(owner),current:copy(context),changed:Boolean(dirty.size&&(review||key(owner)!==key(context)))});
 return {get version(){return sequence;},state,ticket:form=>dirty.get(form),
  touch(form,context){if(!forms.has(form)||!context?.scopeHash)return;if(!owner)owner=copy(context);dirty.set(form,++sequence);},
  saved(form,ticket){if(ticket===undefined||dirty.get(form)!==ticket)return;dirty.delete(form);if(!dirty.size){owner=null;review=false;}},
  requireReview(){if(dirty.size)review=true;},
  assert(context){if(state(context).changed)throw Error('输入仍属于先前的工单或来源版本，请先核对当前工单再提交。');},
  ack(context,bindUnassigned=false){if(owner&&owner.scopeHash!==context?.scopeHash)throw Error('账户已变化，不能重新绑定原账户的输入。');if(owner&&owner.orderId!==(context?.orderId??null)&&!(bindUnassigned&&owner.orderId===null))throw Error('输入属于另一份工单，请返回原工单或明确放弃后重新填写。');if(dirty.size){owner=copy(context);review=false;sequence++;}},
  clear(){owner=null;review=false;dirty.clear();sequence++;},
 };
}
