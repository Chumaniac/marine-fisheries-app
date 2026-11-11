import test from 'node:test';
import assert from 'node:assert/strict';
import {queueOverview} from '../src/queue-view.js';

test('queue shows business actions, dependency waits and retry progress only for current scope',()=>{
 const actor={scopeHash:'a',tenant:'demo',subject:'operator',expiresAt:10000};
 const base={scopeHash:'a',tenant:'demo',subject:'operator',createdAt:100,dependencies:['b'],path:'/v1/batches/b/transitions'};
 const items=[{...base,requestId:'first',blocked:true,lastStatus:409,attempt:2,lastMessage:'PRIVATE_RAW_RESPONSE'}, {...base,requestId:'second',createdAt:101}, {...base,requestId:'other',scopeHash:'different'}];
 const result=queueOverview(items,actor,1000);
 assert.deepEqual(result.counts,{all:2,manual:1,waiting:1,ready:0});
 assert.equal(result.rows[0].label,'更新批次状态');
 assert.match(result.rows[0].hint,/最新状态/);
 assert.equal(result.rows[0].attempts,2);
 assert.match(result.rows[1].hint,/前序/);
 assert.ok(!JSON.stringify(result).includes('PRIVATE_RAW_RESPONSE'));
 assert.deepEqual(queueOverview(items,null,1000).rows,[]);
 assert.deepEqual(queueOverview(items,{...actor,expiresAt:0},1000).rows,[]);
});
