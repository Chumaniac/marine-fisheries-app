import test from 'node:test';
import assert from 'node:assert/strict';
import {MAX_SNAPSHOTS,trackRecentSnapshot} from '../src/snapshots.js';

test('snapshot metadata retains the most recently saved scopes regardless of key order',()=>{
 const recent=[],expired=[];
 for(let i=0;i<19;i++)assert.equal(trackRecentSnapshot(recent,'b-old-'+i,100+i),undefined);
 expired.push(trackRecentSnapshot(recent,'a-recent',999));
 assert.deepEqual(expired,['b-old-0']);assert.equal(recent.length,MAX_SNAPSHOTS-1);
 assert(recent.some(row=>row.key==='a-recent'));
 assert.equal(trackRecentSnapshot(recent,'z-older',1),'z-older');
});

test('invalid legacy timestamps expire before valid entries and ties are deterministic',()=>{
 for(const invalid of [undefined,null,-1,NaN,Infinity,'999',1.5]){
  const recent=[];trackRecentSnapshot(recent,'invalid',invalid);
  for(let i=0;i<18;i++)trackRecentSnapshot(recent,'valid-'+i,0);
  assert.equal(trackRecentSnapshot(recent,'newest',1),'invalid');
 }
 const recent=[];for(let i=0;i<19;i++)trackRecentSnapshot(recent,'equal-'+i,10);
 assert.equal(trackRecentSnapshot(recent,'equal-last',10),'equal-last');
});

test('metadata stays bounded for legacy caches larger than the current capacity',()=>{
 const recent=[],expired=[];for(let i=0;i<1000;i++){
  const key=trackRecentSnapshot(recent,String(i),i);if(key!==undefined)expired.push(key);
  assert(recent.length<=19);
 }
 assert.equal(expired.length,981);assert.deepEqual(recent.map(row=>row.saved),Array.from({length:19},(_,i)=>999-i));
 assert(recent.every(row=>Object.keys(row).sort().join(',')==='key,saved'));
});
