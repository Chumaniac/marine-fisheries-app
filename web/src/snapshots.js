export const MAX_SNAPSHOTS=20;

// Retain at most 19 metadata entries alongside the current scope. Invalid
// legacy timestamps expire first; equal timestamps keep cursor order.
export function trackRecentSnapshot(recent,key,saved){
 recent.push({key,saved:Number.isSafeInteger(saved)&&saved>=0?saved:-1});
 recent.sort((a,b)=>b.saved-a.saved);
 return recent.length>=MAX_SNAPSHOTS?recent.pop().key:undefined;
}

export function saveSnapshot(database,key,data,saved=Date.now()){
 return new Promise((resolve,reject)=>{
  const transaction=database.transaction('snapshots','readwrite'),store=transaction.objectStore('snapshots'),recent=[];
  transaction.oncomplete=()=>resolve();
  transaction.onerror=transaction.onabort=()=>reject(transaction.error||new Error('SNAPSHOT_WRITE_FAILED'));
  store.put({data,saved},key);
  // One atomic write/prune transaction and one cursor payload at a time;
  // never copy all dashboard bodies into an in-memory array.
  store.openCursor().onsuccess=event=>{
   const cursor=event.target.result;if(!cursor)return;
   if(cursor.key!==key){const expired=trackRecentSnapshot(recent,cursor.key,cursor.value?.saved);if(expired!==undefined)store.delete(expired);}
   cursor.continue();
  };
 });
}
