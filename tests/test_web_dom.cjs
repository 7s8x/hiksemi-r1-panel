// Run actual incremental handlers with a minimal DOM; no npm/browser dependencies.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(require('node:path').join(__dirname,'../r1_web.html'),'utf8');
const code=source.slice(source.indexOf('// Incremental public SVG:'),source.indexOf('syncTheme().finally'));
let writes=0,mounts=0,requests=[],timers=[],failure=false,visibility;
function node(){return {firstChild:{set nodeValue(v){this.value=v;writes++}},setAttribute(k,v){writes++},style:{}}}
const elements={};
for(const id of ['publicPanel','publicFans','webPanel','webFans']){
  elements[id]={style:{},replaceChildren(svg){mounts++;this.svg=svg}};
  elements[id+'Status']={};
}
elements.public={classList:{contains:()=>false}};
elements.app={classList:{contains:()=>true}};
const spec={id:'one',pages:{}};
for(const page of ['panel','fans'])spec.pages[page]={svg:'<svg/>',bindings:[['a','text'],['b','width']]};
let values={id:'one',valid_for:30,pages:{panel:['10','20'],fans:['30','40']}};
const document={hidden:false,addEventListener(n,f){visibility=f},importNode(){
  const nodes={a:node(),b:node()};
  return {style:{},querySelector(q){return nodes[q.includes('"a"')?'a':'b']}};
}};
const context={document,$:id=>elements[id],performance:{now:()=>0},Map,Number,Error,
  DOMParser:class{parseFromString(){return {querySelector:()=>null,documentElement:{}}}},
  AbortController:class{signal={};abort(){}},
  setTimeout(f,ms){timers.push([f,ms]);return timers.length},clearTimeout(){},
  async fetch(url){requests.push(url);return {ok:!failure,json:async()=>url.includes('layout')?spec:values}}};
vm.createContext(context);vm.runInContext(code,context);
(async()=>{
  await context.refreshWebViews();assert.equal(mounts,2);assert.equal(writes,4);
  assert.equal(requests.filter(u=>u.includes('layout')).length,1);
  await context.refreshWebViews();assert.equal(mounts,2);assert.equal(writes,4,'unchanged values produce no DOM writes');
  values.pages.panel[0]='11';await context.refreshWebViews();assert.equal(writes,5,'only changed text written');
  assert.equal(mounts,2,'numbers never remount layout');
  values.pages.panel[0]='<script>bad</script>';await context.refreshWebViews();assert.equal(writes,6,'data remains plain text');
  failure=true;await context.refreshWebViews();assert.equal(elements.publicPanel.style.visibility,'hidden');
  failure=false;await context.refreshWebViews();assert.equal(mounts,2,'recovery reuses fixed nodes');
  timers.filter(t=>t[1]===30000).at(-1)[0]();assert.equal(elements.publicFans.style.visibility,'hidden','expiry hides stale image');
  document.hidden=true;const count=requests.length;visibility();await context.refreshWebViews();assert.equal(requests.length,count);
  document.hidden=false;values.id='different';await context.refreshWebViews();assert.equal(elements.publicPanel.style.visibility,'hidden','layout/data race fails closed');
  values.id=spec.id='two';await context.refreshWebViews();assert.equal(mounts,4,'new theme/version remounts once');
  console.log('web_incremental_dom=passed');
})().catch(e=>{console.error(e);process.exitCode=1});
