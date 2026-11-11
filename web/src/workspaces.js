const names = new Set(['overview','collection','trace','review','operations']);

/** Navigation only: retain all form nodes and never perform a business request. */
export function createWorkspaces({document,window,onChange=()=>{}}) {
  const panels=[...document.querySelectorAll('main [data-workspace]')];
  const links=[...document.querySelectorAll('.task-navigation a')];
  const anchors=[...document.querySelectorAll('a[href^="#"]')];
  const grids=[...document.querySelectorAll('[data-workspace-grid]')];
  let current=null;
  function resolve(hash){
    let id;try{id=decodeURIComponent((hash||'').replace(/^#/,''));}catch{return null;}
    const target=document.getElementById(id);if(!target)return null;
    const name=target.closest('[data-workspace]')?.dataset.workspace;
    return {target,name:names.has(name)?name:current||'overview'};
  }
  function show(name,target=null){
    if(!names.has(name))return false;
    const changed=current!==name;current=name;
    for(const panel of panels)panel.hidden=panel.dataset.workspace!==name;
    for(const grid of grids){const visible=[...grid.children].filter(child=>!child.hidden);grid.hidden=!visible.length;grid.dataset.single=String(visible.length===1);}
    document.documentElement.dataset.activeWorkspace=name;
    for(const link of links){if(link.dataset.workspaceLink===name)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');}
    if(changed)onChange(name);
    if(target){
      for(let parent=target.parentElement;parent;parent=parent.parentElement)if(parent.tagName==='DETAILS')parent.open=true;
      if(target.closest('[hidden]'))target=target.closest('[data-workspace]')?.querySelector('h2')||target;
      if(target.tabIndex<0&&!target.hasAttribute('tabindex'))target.tabIndex=-1;
      target.focus({preventScroll:true});target.scrollIntoView({block:'start',behavior:'auto'});
    }
    return true;
  }
  const fromHash=()=>{const destination=resolve(window.location.hash);if(destination)show(destination.name,destination.target);};
  const clicks=anchors.map(link=>{const listener=event=>{if(event.defaultPrevented||event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;const destination=resolve(link.getAttribute('href'));if(destination)show(destination.name,destination.target);};link.addEventListener('click',listener);return [link,listener];});
  window.addEventListener('hashchange',fromHash);
  const first=resolve(window.location.hash);show(first?.name||'overview',first?.target||null);
  return {get current(){return current;},show,dispose(){window.removeEventListener('hashchange',fromHash);for(const [link,listener]of clicks)link.removeEventListener('click',listener);}};
}
