import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

async function shellWorker(){
 const handlers=new Map(),stored=new Map();let installed;
 const key=request=>typeof request==='string'?request:new URL(request.url).pathname+new URL(request.url).search;
 const cache={addAll:async urls=>{for(const url of urls)stored.set(url,new Response(url));},put:async(request,value)=>stored.set(key(request),value)};
 const sandbox={URL,Promise,fetch:async()=>{throw Error('offline');},caches:{open:async()=>cache,match:async request=>stored.get(key(request))},self:{location:{origin:'https://marine.test'},addEventListener:(name,fn)=>handlers.set(name,fn),skipWaiting:async()=>{}}};
 vm.runInNewContext(await readFile(new URL('../sw.js',import.meta.url),'utf8'),sandbox);
 handlers.get('install')({waitUntil:task=>{installed=task;}});await installed;
 return handlers.get('fetch');
}
test('offline shell serves every actual HTML entry asset',async()=>{
 const html=await readFile(new URL('../index.html',import.meta.url),'utf8');
 const assets=[...html.matchAll(/<(?:link|script)\b[^>]*(?:href|src)="([^"]+)"/g)].map(match=>match[1]).filter(url=>url.startsWith('/'));
 const fetch=await shellWorker();assert.ok(assets.length>=3);
 for(const asset of assets){let response;fetch({request:{method:'GET',url:'https://marine.test'+asset},respondWith:value=>{response=value;},waitUntil:()=>{}});assert.ok(response,'No offline handler for '+asset);assert.equal(await (await response).text(),asset);}
});
test('offline shell never intercepts private API requests',async()=>{
 const fetch=await shellWorker();let intercepted=false;
 fetch({request:{method:'GET',url:'https://marine.test/v1/session'},respondWith:()=>{intercepted=true;}});
 assert.equal(intercepted,false);
});
