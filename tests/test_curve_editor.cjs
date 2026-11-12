// Run the shipped handlers with a delayed API reply, without a browser or device.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../r1_web.html'),'utf8');
const save=source.slice(source.indexOf('function setCurveSaving('),source.indexOf("document.querySelectorAll('[data-page]')"));
const change=source.split('\n').find(line=>line.startsWith("$('curveProfile').onchange="));
function fixture(){
 const elements=Object.fromEntries(['saveCurves','resetCurves','curveProfile','curveMessage','cpuPlot','drivePlot'].map(id=>[id,{setAttribute(){}}]));
 const controls=[{},{}];
 const high={cpu:[[55,130]],drive:[[35,130]]},low={cpu:[[55,120]],drive:[[35,120]]};
 let resolve,reject,calls=0;
 const context={$:id=>elements[id],activeProfile:'high',curveSaving:false,activeHandle:null,profiles:{high,low},curves:high,
  document:{querySelectorAll:()=>controls},curveWarning:()=>'',confirm:()=>true,
  readCurve:name=>structuredClone(high[name]),drawCurve:()=>{},api:()=>{calls++;return new Promise((ok,bad)=>{resolve=ok;reject=bad})}};
 vm.createContext(context);vm.runInContext(save+'\n'+change,context);
 return {context,elements,controls,high,low,reply:value=>resolve(value),fail:error=>reject(error),calls:()=>calls};
}
(async()=>{
 let f=fixture(),task=f.elements.saveCurves.onclick();
 assert.equal(f.context.curveSaving,true);assert.equal(f.elements.curveProfile.disabled,true);
 assert.ok(f.controls.every(control=>control.disabled));
 await f.elements.saveCurves.onclick();assert.equal(f.calls(),1,'no duplicate submission');
 f.elements.curveProfile.value='low';f.elements.curveProfile.onchange();
 assert.equal(f.context.activeProfile,'high','switch blocked while saving');
 f.context.activeProfile='low'; // Even an external state change must not redirect the receipt.
 f.reply({curves:f.high,profile:'high',curve_id:'new-high'});await task;
 assert.equal(f.context.profiles.low,f.low);assert.equal(f.context.curveSaving,false);
 assert.equal(f.elements.curveProfile.disabled,false);assert.ok(f.controls.every(control=>!control.disabled));
 f=fixture();task=f.elements.saveCurves.onclick();f.fail(Error('rollback unconfirmed'));await task;
 assert.equal(f.context.curveSaving,false);assert.equal(f.context.profiles.low,f.low);
 assert.match(f.elements.curveMessage.textContent,/rollback unconfirmed/);
 f=fixture();task=f.elements.saveCurves.onclick();f.reply({curves:f.low,profile:'low',curve_id:'wrong'});await task;
 assert.equal(f.context.profiles.high,f.high);assert.match(f.elements.curveMessage.textContent,/档位不一致/);
 // Parse the complete inline script as well as executing the production handlers.
 new vm.Script(source.match(/<script>([\s\S]*?)<\/script>/)[1]);
 console.log('curve_editor_regressions=passed');
})().catch(error=>{console.error(error);process.exitCode=1});
