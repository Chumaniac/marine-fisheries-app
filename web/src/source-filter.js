/** Filter one server-scoped page; selected rows remain available for review. */
export function filterSourceEvents(events,filters={},selected=[]){
 if(!Array.isArray(events)||events.length>500||!Array.isArray(selected)||selected.length>500)throw Error('来源筛选每页最多500条。');
 const type=filters.type??'all',query=filters.query??'';
 if(!['all','fish.observation','environment.reading'].includes(type))throw Error('来源类型筛选无效。');
 if(typeof query!=='string'||query.length>80)throw Error('来源搜索最多80个字符。');
 const term=query.trim().toLocaleLowerCase(),chosen=new Set(selected),rows=[],retainedSelected=[];
 let matched=0;
 for(const event of events){
  const matches=(type==='all'||event.eventType===type)&&(!filters.imagesOnly||event.imageAvailable===true)&&(!term||[event.eventId,event.deviceId,event.occurredAt].some(value=>String(value??'').toLocaleLowerCase().includes(term)));
  if(matches)matched++;
  if(matches||chosen.has(event.eventId)){rows.push(event);if(!matches)retainedSelected.push(event.eventId);}
 }
 return {rows,matched,retainedSelected};
}
