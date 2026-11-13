// Actual mirror handlers with a fake clock: fixed 5s cadence, immediate controls.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../r1_web.html'),'utf8');
const handlers=source.slice(source.indexOf('let mirrorTimer='),source.indexOf('async function refresh(){'));
let writes=0,refreshes=0,src=null,hiddenApp=false,visibility,now=0,next=0;
const timers=new Map();
const screen={removeAttribute(name){assert.equal(name,'src');src=null},set src(value){src=value;writes++}};
const details={open:false};
const elements={screen,mirrorDetails:details,app:{classList:{contains:()=>hiddenApp}}};
const document={hidden:false,addEventListener(name,fn){assert.equal(name,'visibilitychange');visibility=fn}};
const context={$:id=>elements[id],document,refresh(){refreshes++},Date:{now:()=>now},JSON,
 setTimeout(fn,delay){const id=++next;timers.set(id,{fn,due:now+delay});return id},
 clearTimeout(id){timers.delete(id)}};
function advance(ms){const until=now+ms;while(true){const [id,t]=[...timers].sort((a,b)=>a[1].due-b[1].due)[0]||[];if(!t||t.due>until)break;now=t.due;timers.delete(id);t.fn()}now=until}
vm.createContext(context);vm.runInContext(handlers,context);
const fans={state:'fans',brightness:80};
assert.match(source,/<details id="mirrorDetails" class="mirror-details">/);
for(let i=0;i<10;i++)context.updateMirror(fans);
assert.equal(writes,0,'collapsed never fetches');
details.open=true;details.ontoggle();assert.equal(refreshes,1);
context.updateMirror(fans);assert.equal(writes,1);assert.match(src,/^\/api\/frame\.png\?t=/);
for(let i=0;i<3;i++){advance(1500);context.updateMirror(fans)}
assert.equal(writes,1,'1.5s status polls do not trigger image encoding');
advance(499);assert.equal(writes,1);advance(1);assert.equal(writes,2,'5s timer fetches changing live contents');
context.updateMirror({...fans,brightness:60});assert.equal(writes,3,'brightness change immediate');
context.updateMirror({state:'panel',brightness:60});assert.equal(writes,4,'page change immediate');
context.updateMirror({state:'panel',brightness:60,updated:now+1});assert.equal(writes,4,'heartbeat timestamp is not a visible change');
screen.onerror();assert.equal(src,null);advance(4999);assert.equal(writes,4);advance(1);assert.equal(writes,5,'image failure waits for bounded cadence');
details.open=false;details.ontoggle();assert.equal(src,null);advance(15000);assert.equal(writes,5);assert.equal(timers.size,0);
context.updateMirror(fans);assert.equal(writes,5,'late reply after collapse cannot fetch');
details.open=true;document.hidden=true;visibility();context.updateMirror(fans);advance(15000);assert.equal(writes,5);
document.hidden=false;visibility();assert.equal(refreshes,2);context.updateMirror(fans);assert.equal(writes,6,'reopen fetches immediately');
context.updateMirror(null);advance(10000);assert.equal(src,null);assert.equal(writes,6,'offline cancels timer');
hiddenApp=true;context.updateMirror(fans);details.ontoggle();assert.equal(writes,6);
assert.ok(source.indexOf('</details>')<source.indexOf('id="pageButtons"'));
assert.match(source,/catch\(e\)\{clearMirror\(\);if\(e.message==='login required'/);
new vm.Script(source.match(/<script>([\s\S]*?)<\/script>/)[1]);
console.log('mirror_5s_and_immediate_controls=passed');
