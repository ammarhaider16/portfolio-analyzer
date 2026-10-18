// Optional real-browser smoke test. Requires a local Chrome installation and running dashboard.
// CHROME_PATH and PORTFOLIO_SMOKE_URL can override the defaults. No npm packages are required.
import {spawn} from 'node:child_process';
import {mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';

const root=process.cwd(), url=process.env.PORTFOLIO_SMOKE_URL||'http://127.0.0.1:8000';
const executable=process.env.CHROME_PATH||'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
await mkdir('.tools/browser',{recursive:true});
const chrome=spawn(executable,['--headless=new','--disable-gpu','--disable-extensions','--disable-background-networking',
  '--disable-component-update','--no-first-run','--no-default-browser-check','--remote-debugging-port=9222',
  `--user-data-dir=${path.join(root,'.tools/browser')}`],{stdio:'ignore',windowsHide:true});
let socket;
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
try {
  let target;
  for(let count=0;count<50;count++) {
    try {const response=await fetch('http://127.0.0.1:9222/json/new?about:blank',{method:'PUT'});target=await response.json();break;} catch {await pause(200);}
  }
  if(!target) throw new Error('Chrome debugging endpoint did not start');
  socket=new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});
  let sequence=0; const pending=new Map(), errors=[];
  socket.addEventListener('message',event=>{
    const data=JSON.parse(event.data);
    if(data.id && pending.has(data.id)) {const {resolve,reject}=pending.get(data.id);pending.delete(data.id);data.error?reject(new Error(data.error.message)):resolve(data.result);}
    if(data.method==='Runtime.exceptionThrown') errors.push(data.params.exceptionDetails.text);
  });
  const send=(method,params={})=>new Promise((resolve,reject)=>{const id=++sequence;pending.set(id,{resolve,reject});socket.send(JSON.stringify({id,method,params}));});
  const evaluate=async expression=>{
    const data=await send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
    if(data.exceptionDetails) throw new Error(data.exceptionDetails.exception?.description||data.exceptionDetails.text);
    return data.result.value;
  };
  const waitFor=async expression=>{for(let attempt=0;attempt<100;attempt++){if(await evaluate(expression))return;await pause(100);}throw new Error('Timed out: '+expression);};
  await send('Runtime.enable'); await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1050,deviceScaleFactor:1,mobile:false});
  await send('Page.navigate',{url});
  await waitFor('Boolean(document.querySelector("#run-button")) && typeof state !== "undefined" && state !== null');
  const optional=await evaluate('state.settings.smtp === null && state.settings.schedule.enabled === false');
  if(!optional) throw new Error('Use a fresh smoke data directory with optional integrations disabled');
  await evaluate("api('/api/settings',{search_provider:'disabled',primary_feeds_enabled:false,market_data_enabled:false,alpha_vantage_enabled:false,model_enabled:false}).then(()=>refresh())");
  await evaluate('document.querySelector("#run-form").requestSubmit()');
  await waitFor('Boolean(document.querySelector("#report-frame")?.getAttribute("src"))');
  await waitFor('document.querySelector("#report-frame").contentDocument?.body?.textContent.includes("Portfolio")');
  await evaluate('document.querySelector("#report-card").hidden=true');
  await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false}).then(data=>writeFile('.tools/dashboard.png',Buffer.from(data.data,'base64')));
  await evaluate('document.querySelector(".nav[data-view=preferences]").click(); document.querySelector("#preferences-form").elements.max_searches.value="3"; document.querySelector("#preferences-form").requestSubmit()');
  await waitFor('state.settings.max_searches === 3');
  await evaluate('document.querySelector(".nav[data-view=connections]").click()');
  const files=await send('DOM.getDocument');
  const input=await send('DOM.querySelector',{nodeId:files.root.nodeId,selector:'#import-form input[type=file]'});
  await send('DOM.setFileInputFiles',{nodeId:input.nodeId,files:[path.join(root,'examples/holdings.csv')]});
  await evaluate('document.querySelector("#import-form").requestSubmit()');
  await waitFor('document.querySelector("#confirm-import").hidden === false');
  await waitFor('portfolioDirty && document.querySelector("#holdings-rows").children.length === 2');
  const initialRuns=await evaluate('state.history.length');
  await evaluate('document.querySelector("#run-form").requestSubmit()');
  await waitFor('document.querySelector("#notice").textContent.includes("Save or discard")');
  if(await evaluate('state.history.length') !== initialRuns) throw new Error('Unsaved draft started a run');
  await evaluate(`document.querySelector('#portfolio-cash').value='$2,345.67';document.querySelector('#portfolio-cash').dispatchEvent(new Event('input'));document.querySelector('#add-holding').click();`);
  await evaluate(`const inputs=document.querySelector('#holdings-rows').lastElementChild.querySelectorAll('input');inputs[0].value='MSFT';inputs[1].value='3';inputs[2].value='';inputs[0].dispatchEvent(new Event('input'));`);
  await evaluate(`document.querySelector('#add-holding').click();document.querySelector('#holdings-rows').lastElementChild.querySelector('button').click();`);
  if(await evaluate('document.querySelector("#holdings-rows").children.length') !== 3) throw new Error('Add/remove holding failed');
  await evaluate('document.querySelector("#portfolio-form").requestSubmit()');
  await waitFor('state.snapshot?.source === "manual" && !portfolioDirty');
  const saved=await evaluate('JSON.stringify(state.snapshot)');
  if(!(await evaluate('state.snapshot.cash === "2345.67" && state.snapshot.positions.length === 3 && state.snapshot.positions[2].symbol === "MSFT" && state.snapshot.positions[2].price === null'))) throw new Error('Saved edits do not match draft');
  await evaluate(`document.querySelector('#portfolio-cash').value='77';document.querySelector('#portfolio-cash').dispatchEvent(new Event('input'));refresh();`);
  await waitFor('state !== null');
  if(await evaluate('document.querySelector("#portfolio-cash").value') !== '77') throw new Error('Refresh overwrote unsaved cash');
  await evaluate('document.querySelector("#discard-portfolio").click()');
  await send('Page.reload');
  await waitFor('typeof state !== "undefined" && state?.snapshot?.source === "manual" && !portfolioDirty');
  if(await evaluate('JSON.stringify(state.snapshot)') !== saved) throw new Error('Saved portfolio changed on reload');
  await evaluate('document.querySelector("#run-form").requestSubmit()');
  await waitFor(`state.history.length > ${initialRuns} && !state.history.some(row=>['queued','running'].includes(row.status))`);
  const storedReport=await evaluate('state.history[0].status');
  if(storedReport==='failed') throw new Error('Saved portfolio briefing failed');
  await evaluate('document.querySelector("#report-card").hidden=true');
  await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await evaluate('window.scrollTo(0,0)');
  await pause(200);
  const noOverflow=await evaluate('document.documentElement.scrollWidth <= window.innerWidth');
  if(!noOverflow) throw new Error('Mobile layout overflows horizontally');
  await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false}).then(data=>writeFile('.tools/dashboard-mobile.png',Buffer.from(data.data,'base64')));
  if(errors.length) throw new Error('Browser exceptions: '+errors.join(', '));
  console.log(JSON.stringify({demoReport:true,emailAndScheduleOptional:optional,preferencesSaved:true,csvImport:true,editableCash:true,addRemoveHoldings:true,unsavedRunBlocked:true,portfolioPersists:true,mobileLayout:true,browserErrors:errors}));
} finally {socket?.close();chrome.kill();}
