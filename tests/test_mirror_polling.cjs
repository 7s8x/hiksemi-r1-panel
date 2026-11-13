// Exercise production mirror handlers: no image fetch while closed/hidden/offline.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../r1_web.html'),'utf8');
const handlers=source.slice(source.indexOf('function clearMirror('),source.indexOf('async function refresh(){'));
let writes=0,refreshes=0,src=null,hiddenApp=false,visibility;
const screen={removeAttribute(name){assert.equal(name,'src');src=null},set src(value){src=value;writes++}};
const details={open:false};
const elements={screen,mirrorDetails:details,app:{classList:{contains:()=>hiddenApp}}};
const document={hidden:false,addEventListener(name,fn){assert.equal(name,'visibilitychange');visibility=fn}};
const context={$:id=>elements[id],document,refresh(){refreshes++},Date};
vm.createContext(context);vm.runInContext(handlers,context);
assert.match(source,/<details id="mirrorDetails" class="mirror-details">/);
for(let i=0;i<10;i++)context.updateMirror({state:'fans'});
assert.equal(writes,0,'default collapsed: repeated status polls must not fetch PNG');
details.open=true;details.ontoggle();assert.equal(refreshes,1);
context.updateMirror({state:'fans'});assert.equal(writes,1);assert.match(src,/^\/api\/frame\.png\?t=/);
details.open=false;details.ontoggle();assert.equal(src,null);
context.updateMirror({state:'fans'});assert.equal(writes,1,'late status reply after collapse stays inactive');
details.open=true;document.hidden=true;visibility();context.updateMirror({state:'fans'});assert.equal(writes,1);
document.hidden=false;visibility();assert.equal(refreshes,2);
context.updateMirror(null);assert.equal(src,null);assert.equal(writes,1,'offline panel has no frame request');
hiddenApp=true;context.updateMirror({state:'fans'});details.ontoggle();assert.equal(writes,1);
// Controls remain outside the collapsed mirror, rather than becoming inaccessible.
assert.ok(source.indexOf('</details>')<source.indexOf('id="pageButtons"'));
assert.match(source,/updateMirror\(panel\);/);
new vm.Script(source.match(/<script>([\s\S]*?)<\/script>/)[1]);
console.log('mirror_on_demand=passed');
