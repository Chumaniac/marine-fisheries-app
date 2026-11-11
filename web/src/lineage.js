const ids=value=>typeof value==='string'&&/^[A-Za-z0-9_-]{1,128}$/.test(value);
const number=(value,min,max)=>Number.isSafeInteger(value)&&value>=min&&value<=max;
const states=['CREATED','LANDED','PROCESSED','CONSUMED','IN_COLD_CHAIN','DELIVERED','RECALLED'];
const reasons={scan_limit:'读取范围达到上限',metadata_unavailable:'部分记录元数据未能核对',missing_links:'部分上游关联不可读取',depth_limit:'到达所选层级',node_limit:'批次数量达到上限',edge_limit:'关系数量达到上限',cycle:'检测到循环关联'};
const relationNames={root:'起点',ancestor:'上游',descendant:'下游'};
export function validLineage(data,root,direction){
 if(!data||data.schema!=='marine.batch-lineage/v1'||data.rootId!==root||data.direction!==direction||data.evidence!=='tenant-local-metadata'||data.ledgerVerified!==false||data.mutated!==false||typeof data.complete!=='boolean'||typeof data.cycleDetected!=='boolean')return false;
 const limits=data.limits;
 if(!limits||!number(limits.nodes,1,100)||limits.edges!==200||!number(limits.depth,1,12)||limits.scan!==1000||limits.recordBytes!==65536||!number(data.scanned,1,1000)||!number(data.unresolvedLinks,0,10000))return false;
 if(!Array.isArray(data.reasons)||data.reasons.length>7||new Set(data.reasons).size!==data.reasons.length||data.reasons.some(value=>!Object.hasOwn(reasons,value))||data.complete!==(data.reasons.length===0)||data.cycleDetected!==data.reasons.includes('cycle'))return false;
 if(!Array.isArray(data.nodes)||data.nodes.length<1||data.nodes.length>limits.nodes||data.scanned<data.nodes.length||!Array.isArray(data.edges)||data.edges.length>200)return false;
 const allowed=direction==='ancestors'?['root','ancestor']:direction==='descendants'?['root','descendant']:['root','ancestor','descendant'];
 if(!['both','ancestors','descendants'].includes(direction)||data.nodes.some(node=>!node||!ids(node.batchId)||typeof node.available!=='boolean'||!number(node.depth,0,limits.depth)||!Array.isArray(node.relations)||node.relations.length<1||node.relations.length>2||new Set(node.relations).size!==node.relations.length||node.relations.some(value=>!allowed.includes(value))||(node.batchId===root?(node.depth!==0||node.relations.join(',')!=='root'):node.depth===0||node.relations.includes('root'))||(node.available?!states.includes(node.state)||!number(node.revision,1,Number.MAX_SAFE_INTEGER)||node.ownerMSP!==null&&!['Org1MSP','Org2MSP','Org3MSP'].includes(node.ownerMSP):node.state!=='UNAVAILABLE'||node.revision!==null||node.ownerMSP!==null)))return false;
 const nodes=new Set(data.nodes.map(node=>node.batchId));
 if(!data.edges.every(edge=>edge&&nodes.has(edge.parent)&&nodes.has(edge.child)))return false;
 return nodes.size===data.nodes.length&&nodes.has(root)&&new Set(data.edges.map(edge=>edge.parent+'|'+edge.child)).size===data.edges.length;
}

/** The independent read point never changes the operation's selected batch. */
export function createLineageView({root,request,getScope,selectedBatch,stateNames={},orgNames={}}){
 const doc=root.ownerDocument,$=selector=>root.querySelector('[data-lineage-'+selector+']');
 const fields={id:$('root'),direction:$('direction'),depth:$('depth'),status:$('status'),result:$('result'),load:$('load')};
 let serial=0,pending=null;
 const node=(tag,text)=>{const element=doc.createElement(tag);if(text!==undefined)element.textContent=text;return element;};
 function clear(reset=false){serial++;pending?.abort();pending=null;fields.load.disabled=false;fields.result.replaceChildren();fields.result.hidden=true;delete fields.result.dataset.complete;fields.status.textContent='';if(reset){fields.id.value='';fields.direction.value='both';fields.depth.value='8';}}
 for(const name of ['id','depth'])fields[name].addEventListener('input',()=>clear());
 fields.direction.addEventListener('change',()=>clear());
 async function load(value){
  clear();const id=(value||fields.id.value||selectedBatch()).trim(),direction=fields.direction.value,depth=Number(fields.depth.value),scope=getScope();
  if(!scope){fields.status.textContent='请先登录后读取谱系。';return false;}
  if(!ids(id)||!['both','ancestors','descendants'].includes(direction)||!number(depth,1,12)){fields.status.textContent='请填写有效批次编号，显示层级为1–12。';return false;}
  fields.id.value=id;const generation=serial,controller=new AbortController();pending=controller;fields.load.disabled=true;fields.status.textContent='正在只读读取上下游关联…';
  try{
   const data=await request('/v1/batches/'+encodeURIComponent(id)+'/lineage?'+new URLSearchParams({direction,max_depth:String(depth)}),{signal:controller.signal,timeoutMs:15000});
   if(controller.signal.aborted||generation!==serial||getScope()!==scope)return false;
   if(!validLineage(data,id,direction))throw Error('谱系响应不完整，请重试；没有改变批次或队列。');
   fields.result.append(node('h3','谱系起点：'+id),node('p','本次显示 '+data.nodes.length+' 个批次、'+data.edges.length+' 条关系。只读展开不改变操作批次。'));
   const wrap=node('div');wrap.className='lineage-table';wrap.tabIndex=0;wrap.setAttribute('role','region');wrap.setAttribute('aria-label','本次读取的谱系批次');
   const table=node('table'),head=node('thead'),heading=node('tr');for(const title of ['批次','相对起点','记录状态','机构 / 版本','继续读取'])heading.append(node('th',title));head.append(heading);table.append(head);
   const body=node('tbody');
   for(const item of data.nodes){const row=node('tr');row.dataset.batchId=item.batchId;row.append(node('th',item.batchId),node('td',item.relations.map(value=>relationNames[value]).join(' / ')),node('td',stateNames[item.state]||(item.available?item.state:'元数据未核对')),node('td',(orgNames[item.ownerMSP]||item.ownerMSP||'机构未记录')+' / '+(item.revision===null?'版本未核对':'v'+item.revision)));
    row.children[0].setAttribute('scope','row');const action=node('td'),button=node('button','只读展开');button.type='button';button.setAttribute('aria-label','只读展开 '+item.batchId);button.addEventListener('click',()=>void load(item.batchId));action.append(button);row.append(action);body.append(row);}
   table.append(body);wrap.append(table);fields.result.append(wrap);
   if(data.edges.length){const list=node('ul');list.className='lineage-edges';list.setAttribute('aria-label','本次读取的批次关联');for(const edge of data.edges)list.append(node('li',edge.parent+' → '+edge.child));fields.result.append(list);}
   fields.result.hidden=false;fields.result.dataset.complete=String(data.complete);
   fields.status.textContent=data.complete?'已读取所选方向内的可见本地记录；影响范围仍需人工核对。':'部分谱系，不能当作完整追溯结论：'+data.reasons.map(value=>reasons[value]).join('、')+'。';return true;
  }catch(error){if(generation===serial&&!controller.signal.aborted&&getScope()===scope)fields.status.textContent=error.message||'谱系读取未完成，原批次和未提交操作保持。';return false;}
  finally{if(generation===serial){pending=null;fields.load.disabled=false;}}
 }
 return {load,clear};
}
