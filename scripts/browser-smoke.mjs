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
  if(!(await evaluate('document.title.includes("Betafolio") && Boolean(document.querySelector(".brand-icon")) && Boolean(document.querySelector("#fill-details"))'))) throw new Error('Betafolio branding or draft enrichment action is missing');
  await evaluate(`state.secrets_present.tavily_api_key=true;state.secrets.tavily_api_key={present:true,source:'keyring',saved_at:new Date().toISOString(),provider:null,editable:true,managed_by:null,env:['PORTFOLIO_SECRET_TAVILY_API_KEY']};renderSecretFields(true)`);
  if(!(await evaluate('document.querySelector("#research-form input[name=key]").hidden && /key saved/i.test(document.querySelector("#tavily-fields .secret-status").textContent)'))) throw new Error('Saved research secret is not masked');
  await evaluate('document.querySelector("#tavily-fields .secret-status .secret-change").click()');
  if(!(await evaluate('!document.querySelector("#research-form input[name=key]").hidden'))) throw new Error('Saved secret cannot be replaced');
  if(await evaluate('state.secrets.model_api_key && !state.secrets.model_api_key.editable')){
    if(!(await evaluate('document.querySelector("#model-form .secret-status").textContent.includes("Managed by") && document.querySelector("#model-form .secret-status .secret-change").hidden'))) throw new Error('Deployment-managed secret is not labelled');
  }
  const optional=await evaluate('state.settings.smtp === null && state.settings.schedule.enabled === false');
  if(!optional) throw new Error('Use a fresh smoke data directory with optional integrations disabled');
  await evaluate("api('/api/settings',{search_provider:'disabled',primary_feeds_enabled:false,market_data_enabled:false,alpha_vantage_enabled:false,model_enabled:false}).then(()=>refresh())");
  await evaluate('document.querySelector("#run-form").requestSubmit()');
  await waitFor('Boolean(document.querySelector("#report-frame")?.getAttribute("src"))');
  if(!(await evaluate('Boolean(document.querySelector("#report-card").compareDocumentPosition(document.querySelector("#portfolio-editor")) & Node.DOCUMENT_POSITION_FOLLOWING)'))) throw new Error('Report is below the portfolio editor');
  await evaluate('document.querySelector("#report-detail").open=true');
  await waitFor('Boolean(document.querySelector("#report-frame").contentDocument?.body?.textContent.trim())');
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
  if(!(await evaluate('document.activeElement === document.querySelector("#holdings-rows input[data-field=quantity]")'))) throw new Error('Import did not focus the first missing share count');
  if(!(await evaluate('document.querySelector("#draft-progress").textContent.includes("2 share counts needed")'))) throw new Error('Import progress is not visible in the editor');
  const missingShares=await evaluate('Array.from(document.querySelectorAll("#holdings-rows tr")).every(row=>row.querySelector("input[data-field=quantity]").value==="")');
  if(!missingShares) throw new Error('Ticker/price CSV should leave shares unresolved for review');
  const initialRuns=await evaluate('state.history.length');
  await evaluate('document.querySelector("#run-form").requestSubmit()');
  await waitFor('document.querySelector("#notice").textContent.includes("Save or discard")');
  if(await evaluate('state.history.length') !== initialRuns) throw new Error('Unsaved draft started a run');
  await evaluate(`document.querySelector('#portfolio-cash').value='$2,345.67';document.querySelector('#portfolio-cash').dispatchEvent(new Event('input'));document.querySelector('#add-holding').click();`);
  await evaluate(`document.querySelectorAll('#holdings-rows input[data-field=quantity]')[0].value='2';document.querySelectorAll('#holdings-rows input[data-field=quantity]')[1].value='4';`);
  await evaluate(`const newRow=document.querySelector('#holdings-rows').lastElementChild;newRow.querySelector('[data-field=symbol]').value='MSFT';newRow.querySelector('[data-field=quantity]').value='3';newRow.querySelector('[data-field=symbol]').dispatchEvent(new Event('input'));`);
  await evaluate(`document.querySelector('#add-holding').click();document.querySelector('#holdings-rows').lastElementChild.querySelector('button').click();`);
  if(await evaluate('document.querySelector("#holdings-rows").children.length') !== 3) throw new Error('Add/remove holding failed');
  await evaluate('document.querySelector("#portfolio-form").requestSubmit()');
  await waitFor('state.snapshot?.source === "manual" && !portfolioDirty');
  const saved=await evaluate('JSON.stringify(state.snapshot)');
  if(!(await evaluate('state.snapshot.cash === "2345.67" && state.snapshot.positions.length === 3 && state.snapshot.positions[2].symbol === "MSFT"'))) throw new Error('Saved edits do not match draft');
  await evaluate(`document.querySelector('#portfolio-cash').value='77';document.querySelector('#portfolio-cash').dispatchEvent(new Event('input'));refresh();`);
  await waitFor('state !== null');
  if(await evaluate('document.querySelector("#portfolio-cash").value') !== '77') throw new Error('Refresh overwrote unsaved cash');
  await evaluate('document.querySelector("#discard-portfolio").click()');
  await send('Page.reload');
  await pause(350);
  await waitFor('typeof state !== "undefined" && state?.snapshot?.source === "manual" && !portfolioDirty');
  if(await evaluate('JSON.stringify(state.snapshot)') !== saved) throw new Error('Saved portfolio changed on reload');
  // A background quote refresh may already have priced MSFT on a reused data directory.
  // Clear that one price so the unpriced-holding coverage check stays deterministic.
  await evaluate(`(async()=>{const status=await api('/api/status');const snapshot=status.snapshot;const holding=(snapshot.positions||[]).find(position=>position.symbol==='MSFT');if(holding&&holding.price!=null){holding.price=null;holding.previous_close=null;await api('/api/portfolio',{snapshot});await refresh();}})()`);
  if(!(await evaluate('state.snapshot.positions.find(position=>position.symbol==="MSFT").price === null'))) throw new Error('Could not set up the unpriced holding');
  await evaluate('document.querySelector("#run-form").requestSubmit()');
  await waitFor(`state.history.length > ${initialRuns} && !state.history.some(row=>['queued','running'].includes(row.status))`);
  const storedReport=await evaluate('state.history[0].status');
  if(storedReport==='failed') throw new Error('Saved portfolio briefing failed');
  await waitFor('document.querySelector("#report-coverage").textContent.includes("2 of 3 holdings valued")');
  if(!(await evaluate('document.querySelector("#value-coverage").textContent.includes("MSFT excluded") && document.querySelector("#allocation-note").textContent.includes("MSFT excluded")'))) throw new Error('Unpriced holding is not named beside allocation');
  if(!(await evaluate('document.querySelector("#cap-events").textContent.includes("Unavailable") && document.querySelector("#cap-model").textContent.includes("Unavailable")'))) throw new Error('Research capability is not explained before running');
  await evaluate('document.querySelector("#report-card").scrollIntoView()');
  await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false}).then(data=>writeFile('.tools/report-desktop.png',Buffer.from(data.data,'base64')));
  await evaluate('document.querySelector("#report-card").hidden=true');
  await send('Emulation.setDeviceMetricsOverride',{width:768,height:900,deviceScaleFactor:1,mobile:false});
  await evaluate('window.scrollTo(0,0)');
  await pause(200);
  if(!(await evaluate('document.documentElement.scrollWidth <= window.innerWidth'))) throw new Error('Tablet layout overflows horizontally');
  await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false}).then(data=>writeFile('.tools/dashboard-tablet.png',Buffer.from(data.data,'base64')));
  await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await evaluate('window.scrollTo(0,0)');
  await pause(200);
  const noOverflow=await evaluate('document.documentElement.scrollWidth <= window.innerWidth');
  if(!noOverflow) throw new Error('Mobile layout overflows horizontally');
  if(!(await evaluate('document.querySelector(".holdings-scroll").scrollWidth <= document.querySelector(".holdings-scroll").clientWidth'))) throw new Error('Mobile holding review needs horizontal scrolling');
  await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false}).then(data=>writeFile('.tools/dashboard-mobile.png',Buffer.from(data.data,'base64')));
  await evaluate('document.querySelector("#report-card").hidden=false;document.querySelector("#report-card").scrollIntoView()');
  await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false}).then(data=>writeFile('.tools/report-mobile.png',Buffer.from(data.data,'base64')));
  if(!(await evaluate('document.querySelector("#report-card h2").textContent==="Latest briefing" && document.querySelectorAll("#report-card h2").length===1'))) throw new Error('Latest briefing title is missing or duplicated');
  if(!(await evaluate('document.querySelector("#report-frame").getAttribute("sandbox").includes("allow-popups")'))) throw new Error('Inline report frame cannot open source links');
  // The dismiss control is shared by every status banner.
  await evaluate(`notice('Smoke notice for dismissal.')`);
  if(!(await evaluate('!document.querySelector("#notice").hidden && Boolean(document.querySelector("#notice .alert-close"))'))) throw new Error('Notice has no dismiss control');
  await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false}).then(data=>writeFile('.tools/notice-desktop.png',Buffer.from(data.data,'base64')));
  await evaluate('document.querySelector("#notice .alert-close").click()');
  if(!(await evaluate('document.querySelector("#notice").hidden'))) throw new Error('Notice did not dismiss');
  if(!(await evaluate('Boolean(document.querySelector("#secret-storage .alert-close")) && Boolean(document.querySelector("#poll-status .alert-close"))'))) throw new Error('Storage or polling status banner lacks a dismiss control');
  // The model key offers the same manage controls as every other secret.
  await evaluate(`state.secrets.model_api_key={present:true,source:'keyring',saved_at:new Date().toISOString(),provider:null,editable:true,managed_by:null,env:['PORTFOLIO_SECRET_MODEL_API_KEY']};renderSecretFields(true)`);
  const parity=await evaluate(`(()=>{const box=document.querySelector('#model-form .secret-status');const vis=el=>Boolean(el)&&!el.hidden;return JSON.stringify({masked:/key saved/i.test(box.querySelector('.secret-status-text').textContent),change:vis(box.querySelector('.secret-change')),remove:vis(box.querySelector('.secret-remove')),test:vis(box.querySelector('.secret-test')),inputHidden:document.querySelector('#model-form input[name=key]').hidden,describedBy:document.querySelector('#model-form input[name=key]').getAttribute('aria-describedby')});})()`);
  if(!/\"masked\":true/.test(parity) || !/\"change\":true/.test(parity) || !/\"remove\":true/.test(parity) || !/\"test\":true/.test(parity) || !/\"inputHidden\":true/.test(parity) || !/secret-status-model_api_key/.test(parity)) throw new Error('Model key does not offer the same manage controls as other secrets: '+parity);
  await evaluate('document.querySelector("#model-form .secret-change").click()');
  if(!(await evaluate('!document.querySelector("#model-form input[name=key]").hidden && !document.querySelector("#model-form .secret-cancel").hidden'))) throw new Error('Change secret does not reveal the replacement field and Cancel');
  await evaluate('document.querySelector("#model-form .secret-cancel").click()');
  if(!(await evaluate('document.querySelector("#model-form input[name=key]").hidden'))) throw new Error('Cancel did not hide the replacement field');
  const removeCall=await evaluate(`(async()=>{const calls=[];const original=window.api;window.api=async(...args)=>{if(args[2]==='DELETE'){calls.push(args);return {ok:true};}return original(...args);};window.confirm=()=>true;document.querySelector('#model-form .secret-remove').click();await new Promise(r=>setTimeout(r,80));window.api=original;return JSON.stringify(calls);})()`);
  if(!removeCall.includes('/api/secrets/model_api_key') || !removeCall.includes('DELETE')) throw new Error('Remove key did not call DELETE /api/secrets/model_api_key: '+removeCall);
  await evaluate(`state.secrets.model_api_key={present:false,source:null,saved_at:null,provider:null,editable:true,managed_by:null,env:['PORTFOLIO_SECRET_MODEL_API_KEY']};renderSecretFields(true)`);
  if(!(await evaluate('document.querySelector("#model-form .secret-status-text").textContent==="No key saved"'))) throw new Error('Model key status did not return to empty after removal');
  // Research history is a bounded scroll region with working per-report Delete and Clear finished.
  await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1050,deviceScaleFactor:1,mobile:false});
  const history=JSON.parse(await evaluate(`(async()=>{state.history=Array.from({length:16},(_,i)=>({id:'smoke-'+i,status:i%4===0?'failed':'success',summary:'Smoke report '+i,error:null,delivery:null,created_at:new Date(Date.now()-i*3600000).toISOString(),schedule_key:null}));renderHistory(state.history);const box=document.querySelector('#history');const bounded=box.scrollHeight>box.clientHeight&&getComputedStyle(box).overflowY==='auto';const rows=box.querySelectorAll('.history-row').length;const calls=[];const original=window.api;window.api=async(...args)=>{if(args[2]==='DELETE'){calls.push(args[0]);return {ok:true};}return original(...args);};window.confirm=()=>true;[...box.querySelectorAll('.history-actions button')].find(b=>b.textContent==='Delete').click();await new Promise(r=>setTimeout(r,60));window.api=original;return JSON.stringify({bounded,rows,deleteCalls:calls});})()`));
  if(!history.bounded || history.rows!==16) throw new Error('Research history is not a bounded scroll region: '+JSON.stringify(history));
  if(!history.deleteCalls.some(url=>url.startsWith('/api/runs/smoke-'))) throw new Error('A report has no working Delete action: '+JSON.stringify(history));
  await evaluate(`state.history=Array.from({length:6},(_,i)=>({id:'smoke-clear-'+i,status:'success',summary:'Clear '+i,error:null,delivery:null,created_at:new Date().toISOString(),schedule_key:null}));renderHistory(state.history)`);
  const clearCalls=JSON.parse(await evaluate(`(async()=>{const calls=[];const original=window.api;window.api=async(...args)=>{if(args[2]==='DELETE'){calls.push(args[0]);return {ok:true};}return original(...args);};window.confirm=()=>true;document.querySelector('#clear-history').click();await new Promise(r=>setTimeout(r,250));window.api=original;return JSON.stringify(calls);})()`));
  if(clearCalls.length!==6 || clearCalls.some(url=>!url.startsWith('/api/runs/smoke-clear-'))) throw new Error('Clear finished did not delete every finished report: '+JSON.stringify(clearCalls));
  await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false}).then(data=>writeFile('.tools/history-desktop.png',Buffer.from(data.data,'base64')));
  await evaluate('state.history=[];renderHistory([])');
  // A huge allocation total must stay on one line and keep its exact value accessible.
  await evaluate(`renderAllocation({as_of:new Date().toISOString(),cash:'0',unsupported:[],positions:[{symbol:'BRK.A',quantity:'100',price:'999999999999',asset_type:'stock'}]})`);
  const chart=JSON.parse(await evaluate(`(()=>{const t=document.querySelector('#chart-total');return JSON.stringify({text:t.textContent,title:t.title,lines:Math.round(t.getBoundingClientRect().height/(parseFloat(getComputedStyle(t).lineHeight)||15)),aria:document.querySelector('#allocation-chart').getAttribute('aria-label')});})()`));
  if(chart.lines!==1 || !chart.title.includes('99,999,999,999') || !chart.aria.includes('99,999,999,999')) throw new Error('Allocation total wrapped or lost its exact value: '+JSON.stringify(chart));
  // Re-confirm the narrow layout after all of the above.
  await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await evaluate('window.scrollTo(0,0)');
  await pause(200);
  if(!(await evaluate('document.documentElement.scrollWidth <= window.innerWidth'))) throw new Error('Mobile layout overflows horizontally after dismissing alerts');
  if(errors.length) throw new Error('Browser exceptions: '+errors.join(', '));
  console.log(JSON.stringify({demoReport:true,emailAndScheduleOptional:optional,preferencesSaved:true,csvImport:true,editableCash:true,addRemoveHoldings:true,unsavedRunBlocked:true,portfolioPersists:true,tabletLayout:true,mobileLayout:true,alertDismissal:true,modelKeyManage:true,reportDelete:true,clearFinished:true,boundedHistory:true,chartSingleLine:true,mobileNoOverflow:true,browserErrors:errors}));
} finally {socket?.close();chrome.kill();}
