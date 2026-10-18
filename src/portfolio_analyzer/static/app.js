"use strict";
const $ = (selector) => document.querySelector(selector);
let state = null, importDraft = null, selectedReport = null, lastAutoReport = null;
const csrf = $('meta[name="csrf-token"]').content;
function notice(message, error=false) {
  const box = $('#notice'); box.textContent = message; box.hidden = false; box.classList.toggle('error', error);
}
async function api(path, body, method) {
  const response = await fetch(path, {method: method || (body === undefined ? 'GET' : 'POST'),
    headers: {'Content-Type':'application/json', 'X-CSRF-Token':csrf},
    body: body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Check the form fields and try again.');
  return data;
}
function action(fn) { return async (event) => { if(event) event.preventDefault(); try { await fn(event); } catch(error) { notice(error.message, true); } }; }
function values(id) { const form=$(id), data=Object.fromEntries(new FormData(form).entries()); for(const element of form.elements) {if(element.disabled && element.name && element.type!=='checkbox') data[element.name]=element.value;} return data; }
function checked(id, name) { return $(id).elements.namedItem(name).checked; }
function setForm(id, data) {
  const form = $(id);
  for (const [name, value] of Object.entries(data || {})) {
    const element = form.elements.namedItem(name); if (!element || element.type === 'password') continue;
    if (element.type === 'checkbox') element.checked = Boolean(value); else element.value = value ?? '';
  }
}
function view(name) {
  document.querySelectorAll('.view').forEach(el => {el.hidden = el.id !== `view-${name}`;});
  document.querySelectorAll('.nav').forEach(el => el.classList.toggle('active', el.dataset.view === name));
  window.scrollTo({top:0, behavior:'smooth'});
}
document.querySelectorAll('[data-view]').forEach(el => el.addEventListener('click', () => view(el.dataset.view)));
function dateLabel(value) { return value ? new Date(value).toLocaleString([], {dateStyle:'medium',timeStyle:'short'}) : 'On demand'; }
function button(label, callback, className='text-button') { const el=document.createElement('button'); el.textContent=label; el.className=className; el.addEventListener('click',action(callback)); return el; }
function openReport(id) {
  selectedReport=id; $('#report-card').hidden=false; $('#report-frame').src=`/reports/${encodeURIComponent(id)}`;
  $('#report-text').href=`/reports/${encodeURIComponent(id)}?plain=true`;
  $('#send-report').hidden=!(state.settings.smtp && state.verified_recipient === state.settings.smtp.recipient);
}

let portfolioDirty = false, editorSnapshot = null, editorVersion = null;
function editStatus(message) { $('#portfolio-edit-status').textContent=message; }
function markPortfolioDirty() { portfolioDirty=true; editStatus('Unsaved changes. Save holdings and cash before starting a briefing.'); }
function localDate(value) { const date=new Date(value); date.setMinutes(date.getMinutes()-date.getTimezoneOffset()); return date.toISOString().slice(0,16); }
function amount(value, label, optional=false) {
  let text=String(value ?? '').trim();
  if (!text && optional) return null;
  text=text.replace(/^\$\s*/, '');
  if (!/^(?:(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?|\.\d+)$/.test(text)) throw new Error(`${label} must be a nonnegative number (for example 1,234.56).`);
  return text.replaceAll(',', '');
}
function addHolding(position={}) {
  const row=document.createElement('tr');
  for(const [name,label] of [['symbol','Symbol'],['quantity','Quantity'],['price','Price'],['asset_type','Type'],['sector','Sector'],['previous_close','Previous close']]) {
    const cell=document.createElement('td'), input=document.createElement('input');
    input.dataset.field=name; input.setAttribute('aria-label',label); input.value=position[name] ?? (name==='asset_type'?'stock':'');
    if(['quantity','price','previous_close'].includes(name)) input.inputMode='decimal';
    if(['symbol','quantity'].includes(name)) input.required=true;
    input.addEventListener('input',markPortfolioDirty);cell.append(input);row.append(cell);
  }
  const cell=document.createElement('td'); const remove=button('Remove',()=>{row.remove();markPortfolioDirty();});remove.type='button';remove.setAttribute('aria-label',`Remove ${position.symbol || 'holding'}`);cell.append(remove);row.append(cell);$('#holdings-rows').append(row);
}
function loadPortfolio(snapshot, draft=false) {
  editorSnapshot=snapshot ? structuredClone(snapshot) : {as_of:new Date().toISOString(),positions:[],cash:'0',source:'manual',account_id:'manual',unsupported:[]};
  $('#holdings-rows').replaceChildren();for(const position of editorSnapshot.positions) addHolding(position);
  $('#portfolio-date').value=localDate(editorSnapshot.as_of);$('#portfolio-cash').value=editorSnapshot.cash;
  portfolioDirty=draft; editorVersion=JSON.stringify(snapshot);
  editStatus(draft?'Import preview: review every holding and the cash balance, then save.':'Saved portfolio. You can edit holdings and cash here.');
}
function collectPortfolio() {
  const positions=[], seen=new Set();
  for(const row of $('#holdings-rows').children) {
    const data={};for(const input of row.querySelectorAll('input')) data[input.dataset.field]=input.value.trim();
    data.symbol=data.symbol.toUpperCase();
    if(!/^[A-Z0-9.\-^=/]{1,40}$/.test(data.symbol)) throw new Error('Provide a valid symbol for every holding.');
    if(seen.has(data.symbol) && !['CASH','USD'].includes(data.symbol) && data.asset_type.toLowerCase()!=='cash') throw new Error(`Duplicate symbol ${data.symbol}; combine lots first.`);
    seen.add(data.symbol);data.quantity=amount(data.quantity,`${data.symbol} quantity`);data.price=amount(data.price,`${data.symbol} price`,true);data.previous_close=amount(data.previous_close,`${data.symbol} previous close`,true);data.sector=data.sector||null;data.asset_type=data.asset_type.toLowerCase()||'stock';positions.push(data);
  }
  const date=new Date($('#portfolio-date').value);if(!Number.isFinite(date.getTime())) throw new Error('Provide a snapshot date and time.');
  return {...editorSnapshot,as_of:date.toISOString(),positions,cash:amount($('#portfolio-cash').value,'Cash balance')};
}

async function refresh(populate=false) {
  state=await api('/api/status');
  const s=state.settings, snapshot=state.snapshot;
  if(!portfolioDirty && JSON.stringify(snapshot)!==editorVersion) loadPortfolio(snapshot);
  $('#snapshot-status').textContent=snapshot ? `${snapshot.positions.length} holdings` : 'Not connected';
  $('#snapshot-time').textContent=snapshot ? `${snapshot.source} · ${dateLabel(snapshot.as_of)}` : 'Try the demo or import your holdings';
  $('#source-label').textContent=snapshot ? snapshot.source.toUpperCase() : 'DEMO AVAILABLE';
  $('#next-run').textContent=dateLabel(state.next_run);
  const latest=state.history[0]; $('#latest-status').textContent=latest ? latest.status.replace('_',' ') : 'No reports yet';
  const active=state.history.some(row => ['queued','running'].includes(row.status));
  $('#run-button').disabled=active; $('#run-button').textContent=active ? 'Research in progress…' : 'Generate briefing →';
  $('#run-email-wrap').hidden=!(s.smtp && state.verified_recipient === s.smtp.recipient);
  $('#email-state').textContent=s.smtp ? (state.verified_recipient===s.smtp.recipient ? `Verified: ${s.smtp.recipient}` : 'Verify your recipient before sending reports.') : 'No email configured';
  $('#logout').hidden=!state.server;
  if (populate) {
    setForm('#model-form',{...s.model,enabled:s.model_enabled});
    setForm('#research-form',s); setForm('#preferences-form',s); setForm('#email-form',s.smtp);
    setForm('#schedule-form',{...s.schedule,scheduled_email:s.scheduled_email});
    if (s.investment_profile) setForm('#profile-form',{...s.investment_profile,allocation_targets:JSON.stringify(s.investment_profile.allocation_targets,null,2), restrictions:(s.investment_profile.restrictions||[]).join('\n')});
    if(snapshot) $('#run-source').value='stored';
    const now=new Date(); now.setMinutes(now.getMinutes()-now.getTimezoneOffset()); $('#import-form').elements.as_of.value=now.toISOString().slice(0,16); $('#image-form').elements.as_of.value=now.toISOString().slice(0,16);
    $('#model-key-state').textContent=state.secrets_present.model_api_key ? 'Credential configured; leave blank to keep' : 'Not configured';
    const managed=state.externally_managed;
    const nested={model:['#model-form',['provider','model','base_url','temperature','max_tokens']],schedule:['#schedule-form',['enabled','delay_minutes','timezone']],smtp:['#email-form',['host','port','username','sender','recipient','tls_mode','plain_text_only','percent_only']]};
    for(const key of managed) {
      if(nested[key]) {const [form,names]=nested[key];for(const name of names) $(form).elements.namedItem(name).disabled=true;}
      else {const selectorName=key==='model_enabled'?'enabled':key;const form=key==='model_enabled'?$('#model-form'):null;document.querySelectorAll(`[name="${selectorName}"]`).forEach(element=>{if(!form||form.contains(element))element.disabled=true;});}
    }
    if(managed.length) $('#managed-settings').textContent=`Managed by your deployment (read-only here): ${managed.join(', ')}.`;
  }
  const history=$('#history'); history.replaceChildren(); history.classList.toggle('empty',!state.history.length);
  if(!state.history.length) history.textContent='Your first briefing starts here. Try the demonstration portfolio above.';
  for(const row of state.history) {
    const item=document.createElement('div'); item.className='history-row';
    const info=document.createElement('div'), title=document.createElement('strong'), detail=document.createElement('small');
    title.textContent=dateLabel(row.created_at); detail.textContent=row.error || (row.schedule_key ? `Scheduled session ${row.schedule_key}` : 'On-demand research');
    if(row.delivery) detail.textContent+=` · Email: ${row.delivery}`;
    info.append(title,detail);
    const actions=document.createElement('div'); actions.className='history-actions';
    const badge=document.createElement('span'); badge.className=`badge ${row.status}`; badge.textContent=row.status; actions.append(badge);
    if(['queued','running'].includes(row.status)) actions.append(button('Cancel', async()=>{await api(`/api/runs/${row.id}/cancel`,{}); await refresh();}));
    else if(row.status!=='failed') actions.append(button('Read report →',()=>{openReport(row.id); $('#report-card').scrollIntoView({behavior:'smooth'});}));
    item.append(info,actions); history.append(item);
  }
  if(latest && ['success','partial'].includes(latest.status) && latest.id!==lastAutoReport && !selectedReport) {openReport(latest.id); lastAutoReport=latest.id;}
}
async function save(body) {for(const key of state.externally_managed) delete body[key];if(!Object.keys(body).length)throw new Error('These settings are managed by your deployment environment.');await api('/api/settings',body); await refresh(); notice('Settings saved.'); }
async function secret(name,value) { if(value) await api(`/api/secrets/${name}`,{value}); }
if($('#login-form')) {
  $('#login-form').addEventListener('submit',action(async()=>{await api('/api/login',values('#login-form')); window.location.reload();}));
} else {
  $('#refresh').addEventListener('click',action(()=>refresh()));
  $('#logout').addEventListener('click',action(async()=>{await api('/api/logout',{});window.location.reload();}));
  $('#run-form').addEventListener('submit',action(async()=>{
    if(portfolioDirty) {view('desk');$('#portfolio-editor').scrollIntoView({behavior:'smooth'});throw new Error('Save or discard your portfolio edits before starting a briefing.');}
    const data=values('#run-form'); data.include_suggestions=checked('#run-form','include_suggestions'); data.send_email=checked('#run-form','send_email');
    await api('/api/runs',data); selectedReport=null; $('#run-form').elements.include_suggestions.checked=false;
    notice('Research started. Your report will appear here.'); await refresh();
  }));
  $('#import-form').addEventListener('submit',action(async()=>{
    const form=$('#import-form'); const file=form.elements.file.files[0]; if(!file || file.size>500000) throw new Error('Choose a CSV smaller than 500 KB.');
    importDraft={csv:await file.text(),as_of:new Date(form.elements.as_of.value).toISOString(),confirm:false};
    const preview=await api('/api/import',importDraft); $('#import-preview').hidden=false;
    loadPortfolio(preview,true); $('#import-preview').textContent=`${preview.positions.length} holdings and $${preview.cash} cash ready for review. Save your edits in Holdings & cash.`; $('#confirm-import').hidden=false; view('desk'); $('#portfolio-editor').scrollIntoView({behavior:'smooth'});
  }));
  $('#confirm-import').addEventListener('click',()=>{view('desk');$('#portfolio-editor').scrollIntoView({behavior:'smooth'});});
  $('#image-form').addEventListener('submit',action(async()=>{
    const form=$('#image-form'), file=form.elements.file.files[0];
    if(!file || file.size>5*1024*1024 || !['image/png','image/jpeg','image/webp'].includes(file.type)) throw new Error('Choose a PNG, JPEG, or WebP screenshot no larger than 5 MB.');
    if(!form.elements.consent.checked) throw new Error('Confirm that this image can be sent to your configured model.');
    const control=$('#image-button'); control.disabled=true; notice('Extracting a draft. You will review and save it yourself.');
    try {
      const encoded=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=()=>reject(new Error('Unable to read this image.'));reader.readAsDataURL(file);});
      const draft=await api('/api/import/image',{image_base64:encoded,content_type:file.type,as_of:new Date(form.elements.as_of.value).toISOString()});
      const {warnings:notes,...snapshot}=draft;
      loadPortfolio(snapshot,true); form.elements.file.value=''; form.elements.consent.checked=false;
      view('desk'); $('#portfolio-editor').scrollIntoView({behavior:'smooth'});
      notice((notes||[]).join('\n') || 'Review every extracted value, fill in missing quantities and cash, then save.');
    } finally {control.disabled=false;}
  }));
  $('#add-holding').addEventListener('click',()=>{addHolding();markPortfolioDirty();});
  $('#portfolio-date').addEventListener('input',markPortfolioDirty);
  $('#portfolio-cash').addEventListener('input',markPortfolioDirty);
  $('#discard-portfolio').addEventListener('click',()=>loadPortfolio(state.snapshot));
  $('#portfolio-form').addEventListener('submit',action(async()=>{
    const snapshot=collectPortfolio();const saved=await api('/api/portfolio',{snapshot});
    loadPortfolio(saved);$('#confirm-import').hidden=true;$('#import-preview').hidden=true;
    await refresh();$('#run-source').value='stored';notice('Holdings and cash saved. Your portfolio is ready for a briefing.');
  }));
  window.addEventListener('beforeunload',event=>{if(portfolioDirty){event.preventDefault();event.returnValue='';}});
  $('#model-provider').addEventListener('change',()=>{const presets={colgate:['openai/gpt-oss-120b','https://litellm.colgate.edu'],openai:['','https://api.openai.com/v1'],anthropic:['','https://api.anthropic.com'],custom:['','http://127.0.0.1:4000/v1']};const [model,base_url]=presets[$('#model-provider').value];setForm('#model-form',{model,base_url});});
  $('#model-form').addEventListener('submit',action(async()=>{const data=values('#model-form');await secret('model_api_key',data.key);await save({model:{provider:data.provider,model:data.model,base_url:data.base_url,temperature:Number(data.temperature),max_tokens:Number(data.max_tokens)},model_enabled:checked('#model-form','enabled')});$('#model-form').elements.key.value='';}));
  $('#research-form').addEventListener('submit',action(async()=>{const data=values('#research-form');await secret('tavily_api_key',data.key);await secret('alpha_vantage_api_key',data.alpha_key);await save({search_provider:data.search_provider,sec_contact:data.sec_contact,primary_feeds_enabled:checked('#research-form','primary_feeds_enabled'),market_data_enabled:checked('#research-form','market_data_enabled'),alpha_vantage_enabled:checked('#research-form','alpha_vantage_enabled')});$('#research-form').elements.key.value='';$('#research-form').elements.alpha_key.value='';}));
  document.querySelectorAll('[data-test]').forEach(el=>el.addEventListener('click',action(async()=>{el.disabled=true;notice('Testing the saved connection…');try{const result=await api(`/api/test/${el.dataset.test}`,{});notice(result.message);}finally{el.disabled=false;}})));
  $('#preferences-form').addEventListener('submit',action(async()=>{const data=values('#preferences-form');for(const key of ['position_review_threshold_pct','sector_review_threshold_pct','max_searches','max_seconds','max_data_age_hours','retention_days']) data[key]=Number(data[key]);await save(data);}));
  $('#profile-form').addEventListener('submit',action(async()=>{const data=values('#profile-form');try{data.allocation_targets=JSON.parse(data.allocation_targets);}catch{throw new Error('Target allocations must be a JSON object, for example {"stocks":80,"cash":20}.');}data.restrictions=data.restrictions.split('\n').map(s=>s.trim()).filter(Boolean);await save({investment_profile:data});}));
  $('#email-form').addEventListener('submit',action(async()=>{const data=values('#email-form');await secret('smtp_password',data.password);delete data.password;data.port=Number(data.port);data.plain_text_only=checked('#email-form','plain_text_only');data.percent_only=checked('#email-form','percent_only');await save({smtp:data,scheduled_email:false});$('#email-form').elements.password.value='';}));
  $('#request-code').addEventListener('click',action(async()=>{const result=await api('/api/email/verification',{});notice(result.message,result.status!=='sent');}));
  $('#verify-form').addEventListener('submit',action(async()=>{await api('/api/email/verify',values('#verify-form'));await refresh();notice('Your inbox is verified. Delivery is now available when you choose it.');}));
  $('#schedule-form').addEventListener('submit',action(async()=>{const data=values('#schedule-form');await save({schedule:{enabled:checked('#schedule-form','enabled'),delay_minutes:Number(data.delay_minutes),timezone:data.timezone},scheduled_email:checked('#schedule-form','scheduled_email')});}));
  $('#send-report').addEventListener('click',action(async()=>{const result=await api(`/api/runs/${selectedReport}/send`,{});notice(result.message,result.status!=='sent');await refresh();}));
  refresh(true).catch(error=>notice(error.message,true));
  setInterval(()=>refresh().catch(error=>notice(error.message,true)),4000);
}
