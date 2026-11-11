import {test} from 'node:test';import assert from 'node:assert/strict';
import {createWorkspaces} from '../src/workspaces.js';
function fixture(hash=''){
 const nodes=new Map(),listeners=new Map(),panels=[],links=[],grids=[],names=['overview','collection','trace','review','operations'],changes=[];
 function node(id,owner=null){const attrs=new Map(),events=new Map();const value={id,dataset:{},hidden:false,tabIndex:-1,parentElement:owner,tagName:'H2',value:'retained input',events,hasAttribute:k=>attrs.has(k),setAttribute(k,v){attrs.set(k,v);},removeAttribute:k=>attrs.delete(k),getAttribute:k=>attrs.get(k),focus(){this.focused=true;},scrollIntoView(){this.scrolled=true;},closest(selector){if(selector==='[data-workspace]')return this.dataset.workspace?this:owner;if(selector==='[hidden]')return this.hidden||owner?.hidden?this:null;return null;},querySelector(){return nodes.get(id+'-heading');},addEventListener(k,fn){events.set(k,fn);},removeEventListener(k,fn){if(events.get(k)===fn)events.delete(k);}};nodes.set(id,value);return value;}
 for(const name of names){const panel=node(name);panel.dataset.workspace=name;panel.tagName='SECTION';panels.push(panel);node(name+'-heading',panel);const link=node('link-'+name);link.dataset.workspaceLink=name;link.setAttribute('href','#'+name+'-heading');links.push(link);}
 const grid={dataset:{},children:[panels[0],panels[1]],hidden:false};grids.push(grid);
 const document={getElementById:id=>nodes.get(id)||null,documentElement:{dataset:{}},querySelectorAll:q=>q==='main [data-workspace]'?panels:q==='[data-workspace-grid]'?grids:links};
 const window={location:{hash},addEventListener(k,fn){listeners.set(k,fn);},removeEventListener(k,fn){if(listeners.get(k)===fn)listeners.delete(k);}};
 const navigation=createWorkspaces({document,window,onChange:name=>changes.push(name)});
 return {navigation,nodes,listeners,panels,links,grid,window,changes};
}
test('a deep link opens only its workspace and retains existing input nodes and values',()=>{
 const f=fixture('#trace-heading'),retained=f.nodes.get('collection-heading');assert.equal(f.navigation.current,'trace');assert.equal(f.grid.hidden,true);assert.deepEqual(f.panels.filter(p=>!p.hidden).map(p=>p.id),['trace']);
 f.navigation.show('collection');assert.equal(f.grid.hidden,false);assert.strictEqual(f.nodes.get('collection-heading'),retained);assert.equal(retained.value,'retained input');assert.equal(f.links[1].getAttribute('aria-current'),'page');
 const before=f.changes.length;f.navigation.show('collection');assert.equal(f.changes.length,before,'reselecting a view never repaints or requests business data');
});
test('hash history and unknown targets remain safe and dispose their listeners',()=>{
 const f=fixture();f.window.location.hash='#review-heading';f.listeners.get('hashchange')();assert.equal(f.navigation.current,'review');assert.equal(f.nodes.get('review-heading').focused,true);
 f.window.location.hash='#bad%ZZ';f.listeners.get('hashchange')();assert.equal(f.navigation.current,'review');assert.equal(f.navigation.show('unknown'),false);
 f.navigation.dispose();assert.equal(f.listeners.size,0);assert.ok(f.links.every(link=>link.events.size===0));
});
test('links open their workspace before normal hash navigation and respect new-tab gestures',()=>{
 const f=fixture(),link=f.links[3],listener=link.events.get('click');listener({button:0,ctrlKey:true});assert.equal(f.navigation.current,'overview');listener({button:0});assert.equal(f.navigation.current,'review');assert.equal(f.nodes.get('review-heading').scrolled,true);
});

test('a direct anchor opens its collapsed ancestors before focusing retained content',()=>{
 const f=fixture(),target=f.nodes.get('operations-heading'),details={tagName:'DETAILS',open:false,parentElement:f.panels[4]};target.parentElement=details;f.navigation.show('operations',target);assert.equal(details.open,true);assert.equal(target.focused,true);
});
