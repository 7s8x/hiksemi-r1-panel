// Execute the shipped quick-control handlers with mocked IPC/HTTP and delayed replies.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../r1_web.html'),'utf8');
const handlers=source.slice(source.indexOf('let webRgbPending='),source.indexOf("$('rgbColor').oninput="));
function fixture({csrf='token',verified=true,read=async()=>({rgb:[2,17,83,219]})}={}){
 const buttons=Object.fromEntries(['webRgbBreathe','webRgbSteady','webRgbOff'].map(id=>[id,{}]));
 const reads=[],writes=[],errors=[];
 const context={csrf,verified,$:id=>buttons[id],api:async url=>{reads.push(url);return read()},setRgb:async(mode,color)=>writes.push([mode,[...color]]),message:text=>errors.push(text)};
 vm.createContext(context);vm.runInContext(handlers,context);
 return {context,buttons,reads,writes,errors};
}
(async()=>{
 for(const [id,mode] of [['webRgbBreathe',3],['webRgbSteady',1],['webRgbOff',0]]){
  const f=fixture();await f.buttons[id].onclick();assert.deepEqual(f.writes,[[mode,[17,83,219]]]);assert.deepEqual(f.reads,['/api/rgb']);
 }
 let f=fixture({verified:false});await f.buttons.webRgbBreathe.onclick();await f.buttons.webRgbSteady.onclick();assert.equal(f.reads.length,0);
 await f.buttons.webRgbOff.onclick();assert.equal(f.writes[0][0],0);
 f=fixture({csrf:''});await f.buttons.webRgbOff.onclick();assert.equal(f.reads.length,0);
 let finish;f=fixture({read:()=>new Promise(resolve=>finish=resolve)});
 const task=f.buttons.webRgbBreathe.onclick();assert.ok(Object.values(f.buttons).every(b=>b.disabled));
 await f.buttons.webRgbSteady.onclick();assert.equal(f.reads.length,1);
 finish({rgb:[1,123,45,67]});await task;assert.deepEqual(f.writes,[[3,[123,45,67]]]);assert.ok(Object.values(f.buttons).every(b=>!b.disabled));
 f=fixture({read:async()=>({rgb:[3,'invalid',0,0]})});await f.buttons.webRgbBreathe.onclick();assert.equal(f.writes.length,0);assert.ok(f.errors.length);assert.equal(f.buttons.webRgbBreathe.disabled,false);
 f=fixture({read:async()=>{throw Error('offline')}});await f.buttons.webRgbOff.onclick();assert.equal(f.writes.length,0);assert.equal(f.buttons.webRgbOff.disabled,false);
 // CSS percentage overlays must match SVG/physical hitboxes on every viewport width.
 function css(name,property){return Number(source.match(new RegExp('\\.web-fan-hit\\.'+name+'\\{([^}]+)\\}'))[1].match(new RegExp(property+':([0-9.]+)%'))[1])}
 assert.ok(Math.abs(css('fan','top')*960/100-657)<.001);
 for(const [name,x] of [['fan1',32],['fan2',138],['fan3',244],['rgb1',32],['rgb2',138],['rgb3',244]]){
  assert.ok(Math.abs(css(name,'left')*376/100-x)<.001,name);
  assert.ok(Math.abs(css(name,'width')*376/100-100)<.001,name);
 }
 assert.ok(Math.abs(css('rgb','top')*960/100-882)<.001);
 console.log('rgb_quick_controls=passed');
})().catch(error=>{console.error(error);process.exitCode=1});
