import test from 'node:test';
import assert from 'node:assert/strict';
import {filterSourceEvents} from '../src/source-filter.js';

const events=Object.freeze([
 Object.freeze({eventId:'fish-a',eventType:'fish.observation',deviceId:'Camera Alpha',occurredAt:'2026-11-04T00:00:00Z',imageAvailable:true}),
 Object.freeze({eventId:'env-b',eventType:'environment.reading',deviceId:'Sensor Beta',occurredAt:'2026-11-04T01:00:00Z',imageAvailable:false}),
 Object.freeze({eventId:'fish-c',eventType:'fish.observation',deviceId:'Camera Gamma',occurredAt:'2026-11-03T00:00:00Z',imageAvailable:false}),
]);
test('filters the current page by event type, retained image and literal metadata query',()=>{
 assert.deepEqual(filterSourceEvents(events,{type:'environment.reading'}).rows.map(row=>row.eventId),['env-b']);
 assert.deepEqual(filterSourceEvents(events,{imagesOnly:true,query:' ALPHA '}).rows.map(row=>row.eventId),['fish-a']);
 assert.equal(filterSourceEvents(events,{query:'<script>'}).matched,0);
 assert.equal(filterSourceEvents(events,{query:'2026-11-03'}).rows[0],events[2]);
 assert.equal(events.length,3);
});
test('keeps explicitly selected current-page sources visible without adding foreign selections',()=>{
 const result=filterSourceEvents(events,{type:'environment.reading'},['fish-a','other-page']);
 assert.deepEqual(result.rows.map(row=>row.eventId),['fish-a','env-b']);
 assert.equal(result.matched,1);assert.deepEqual(result.retainedSelected,['fish-a']);
 assert.deepEqual(filterSourceEvents(events,{query:'missing'},['fish-c']).rows,[events[2]]);
});
test('bounds the input and rejects unknown filter kinds before rendering',()=>{
 assert.deepEqual(filterSourceEvents([],{}),{rows:[],matched:0,retainedSelected:[]});
 assert.throws(()=>filterSourceEvents(Array(501).fill(events[0]),{}),/500/);
 assert.throws(()=>filterSourceEvents(events,{query:'x'.repeat(81)}),/80/);
 assert.throws(()=>filterSourceEvents(events,{type:'unknown'}),/类型/);
});
