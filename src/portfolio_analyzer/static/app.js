"use strict";
const $ = (selector) => document.querySelector(selector);
let state = null, importDraft = null, selectedReport = null, lastAutoReport = null, pendingRunId = null;
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
  document.querySelectorAll('.nav').forEach(el => {const active=el.dataset.view===name;el.classList.toggle('active',active);if(active)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');});
  window.scrollTo({top:0, behavior:'smooth'});
}
document.querySelectorAll('[data-view]').forEach(el => el.addEventListener('click', () => view(el.dataset.view)));
function dateLabel(value) { return value ? new Date(value).toLocaleString([], {dateStyle:'medium',timeStyle:'short'}) : 'On demand'; }
function button(label, callback, className='text-button') { const el=document.createElement('button'); el.textContent=label; el.className=className; el.addEventListener('click',action(callback)); return el; }
async function openReport(id, scroll=false) {
  selectedReport=id; $('#report-card').hidden=false; $('#report-frame').src=`/reports/${encodeURIComponent(id)}`;
  $('#report-text').href=`/reports/${encodeURIComponent(id)}?plain=true`;
  $('#send-report').hidden=!(state.settings.smtp && state.verified_recipient === state.settings.smtp.recipient);
  $('#report-summary').textContent='Loading conclusions…';
  try { const row=await api(`/api/runs/${encodeURIComponent(id)}`); if(selectedReport===id) renderReportSummary(row); if(scroll) $('#report-card').scrollIntoView({behavior:'smooth',block:'start'}); }
  catch(error) { if(selectedReport===id) $('#report-summary').textContent=`Could not load the summary: ${error.message}`; }
}

function renderReportSummary(row) {
  const root=$('#report-summary');root.replaceChildren();
  const result=row.result;if(!result){root.textContent='This report is still running.';return;}
  $('#report-date').textContent=`Positions as of ${dateLabel(result.snapshot_as_of)}`;
  const coverage=result.coverage||{};
  $('#report-coverage').textContent=coverage.summary||'Portfolio measures are available; check the limitations below.';
  const findings=(result.findings||[]).filter(item=>item.title!=='Portfolio overview').sort((a,b)=>Number(Boolean(b.evidence_ids?.length))-Number(Boolean(a.evidence_ids?.length))).slice(0,4);
  if(!findings.length){const empty=document.createElement('p');empty.textContent='No supported conclusion is available for this run. Check the coverage details below.';root.append(empty);}
  const sources=new Map((result.sources||[]).map(source=>[source.id,source]));
  const list=document.createElement('div');list.className='insight-list';
  findings.forEach((finding,index)=>{const article=document.createElement('article');article.className='insight';const number=document.createElement('span');number.className='insight-number';number.textContent=String(index+1).padStart(2,'0');const body=document.createElement('div');const title=document.createElement('h3');title.textContent=finding.title;const detail=document.createElement('p');detail.textContent=finding.detail;body.append(title,detail);const context=[finding.event_date?`Event: ${finding.event_date}`:'',finding.affected_symbols?.length?`Affects: ${finding.affected_symbols.join(', ')}`:'',finding.portfolio_relevance||'',finding.uncertainty?`Limit: ${finding.uncertainty}`:''].filter(Boolean);for(const note of context){const p=document.createElement('p');p.className='insight-context';p.textContent=note;body.append(p);}const links=document.createElement('div');links.className='insight-sources';const ids=(finding.evidence_ids||[]).filter(id=>sources.has(id));ids.forEach((id,i)=>{const source=sources.get(id);try{const url=new URL(source.url);if(!['http:','https:'].includes(url.protocol))return;const anchor=document.createElement('a');anchor.href=url.href;anchor.target='_blank';anchor.rel='noopener noreferrer';anchor.textContent=source.title||source.publisher||`Source ${i+1}`;links.append(anchor);}catch{}});if(links.childElementCount)body.append(links);article.append(number,body);list.append(article);});
  root.append(list);
  if(coverage.issues?.length){const title=document.createElement('h3');title.textContent='What limits this briefing';root.append(title);const issues=document.createElement('ul');issues.className='coverage-issues';for(const issue of coverage.issues.slice(0,5)){const li=document.createElement('li');li.append(document.createTextNode(issue.message+' '));if(issue.action){const action=button(issue.action==='portfolio'?'Fix holdings':'Set up research',()=>goToIssue(issue.action));action.type='button';li.append(action);}issues.append(li);}root.append(issues);}
}

function goToIssue(actionName){if(actionName==='portfolio'){view('desk');$('#portfolio-editor').scrollIntoView({behavior:'smooth'});return;}view('connections');$('#research-setup').open=true;$('#research-setup').scrollIntoView({behavior:'smooth'});}

let portfolioDirty = false, editorSnapshot = null, editorVersion = null, activeImportSource = null;
function editStatus(message) { $('#portfolio-edit-status').textContent=message; }
function updateDraftProgress() {
  const progress=$('#draft-progress');progress.hidden=!activeImportSource;
  if(!activeImportSource)return;
  const missing=[];const names=[];
  for(const row of $('#holdings-rows').children){const fields=[...row.querySelectorAll('input,select')];const quantity=fields.find(field=>field.dataset.field==='quantity');if(quantity&&!quantity.value.trim()){missing.push(quantity);names.push(fields.find(field=>field.dataset.field==='symbol')?.value||'unnamed holding');}}
  const cashMissing=!$('#portfolio-cash').value.trim();
  progress.textContent=`${activeImportSource} draft · positions dated ${dateLabel($('#portfolio-date').value)}. ${missing.length} ${missing.length===1?'share count':'share counts'} needed${names.length?`: ${names.join(', ')}`:''}${cashMissing?'; cash balance needed':''}. Review then save.`;
}
function markPortfolioDirty() { portfolioDirty=true; editStatus('Unsaved changes. Save holdings and cash before starting a briefing.'); updateDraftProgress(); }
function focusFirstMissing(){let input=null;for(const row of $('#holdings-rows').children){input=[...row.querySelectorAll('input,select')].find(field=>field.dataset.field==='quantity'&&!field.value.trim());if(input)break;}(input||(!$('#portfolio-cash').value.trim()?$('#portfolio-cash'):null))?.focus();}
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
  for(const [name,label] of [['symbol','Ticker'],['quantity','Shares'],['price','Price per share in USD']]) {
    const cell=document.createElement('td'), input=document.createElement('input');cell.dataset.label=label;
    input.dataset.field=name; input.setAttribute('aria-label',label); input.value=position[name] ?? '';
    if(['quantity','price','previous_close'].includes(name)) input.inputMode='decimal';
    if(['symbol','quantity'].includes(name)) input.required=true;
    input.addEventListener('input',markPortfolioDirty);cell.append(input);row.append(cell);
  }
  const symbolCell=row.firstElementChild;const details=document.createElement('details');details.className='instrument-details';const summary=document.createElement('summary');summary.textContent='Instrument details';details.append(summary);
  const typeLabel=document.createElement('label');typeLabel.textContent='Type';const type=document.createElement('select');type.dataset.field='asset_type';type.setAttribute('aria-label','Instrument type');for(const [value,text] of [['unknown','Unknown'],['stock','Stock'],['etf','ETF'],['fund','Fund'],['mutual_fund','Mutual fund'],['bond','Bond'],['cash','Cash'],['other','Other']]){const option=document.createElement('option');option.value=value;option.textContent=text;type.append(option);}type.value=position.asset_type || 'unknown';if(!type.value){const option=document.createElement('option');option.value=position.asset_type;option.textContent=position.asset_type;type.append(option);type.value=position.asset_type;}type.addEventListener('change',markPortfolioDirty);typeLabel.append(type);details.append(typeLabel);
  for(const [name,label] of [['instrument_name','Instrument name'],['exchange','Exchange'],['sector','Sector'],['previous_close','Previous close']]){const fieldLabel=document.createElement('label');fieldLabel.textContent=label;const input=document.createElement('input');input.dataset.field=name;input.value=position[name]??'';if(name==='previous_close')input.inputMode='decimal';input.addEventListener('input',markPortfolioDirty);fieldLabel.append(input);details.append(fieldLabel);}symbolCell.append(details);
  const cell=document.createElement('td');cell.dataset.label='Action'; const remove=button('Remove',()=>{row.remove();markPortfolioDirty();});remove.type='button';remove.setAttribute('aria-label',`Remove ${position.symbol || 'holding'}`);cell.append(remove);row.append(cell);$('#holdings-rows').append(row);
}
function loadPortfolio(snapshot, draft=false) {
  editorSnapshot=snapshot ? structuredClone(snapshot) : {as_of:new Date().toISOString(),positions:[],cash:'0',source:'manual',account_id:'manual',unsupported:[]};
  activeImportSource=draft?(snapshot?.source==='image'?'Screenshot':'CSV'):null;
  $('#holdings-rows').replaceChildren();for(const position of editorSnapshot.positions) addHolding(position);
  $('#portfolio-date').value=localDate(editorSnapshot.as_of);$('#portfolio-cash').value=editorSnapshot.cash ?? '';
  portfolioDirty=draft; editorVersion=JSON.stringify(snapshot);
  const missing=editorSnapshot.positions.filter(p=>p.quantity===null||p.quantity===undefined||p.quantity==='').length;
  editStatus(draft?`Import draft: ${missing} ${missing===1?'holding needs':'holdings need'} shares. Check all prices and cash, then save.`:'Saved portfolio. Edit holdings and cash here.');
  updateDraftProgress();
}
function collectPortfolio() {
  const positions=[], seen=new Set();
  for(const row of $('#holdings-rows').children) {
    const data={};for(const input of row.querySelectorAll('input,select')) data[input.dataset.field]=input.value.trim();
    data.symbol=data.symbol.toUpperCase();
    if(!/^[A-Z0-9.\-^=/]{1,40}$/.test(data.symbol)) throw new Error('Provide a valid symbol for every holding.');
    if(seen.has(data.symbol) && !['CASH','USD'].includes(data.symbol) && data.asset_type.toLowerCase()!=='cash') throw new Error(`Duplicate symbol ${data.symbol}; combine lots first.`);
    seen.add(data.symbol);data.quantity=amount(data.quantity,`${data.symbol} shares`);data.price=amount(data.price,`${data.symbol} price`,true);data.previous_close=amount(data.previous_close,`${data.symbol} previous close`,true);data.sector=data.sector||null;data.asset_type=(data.asset_type||'unknown').toLowerCase();positions.push(data);
  }
  const date=new Date($('#portfolio-date').value);if(!Number.isFinite(date.getTime())) throw new Error('Provide a snapshot date and time.');
  return {...editorSnapshot,as_of:date.toISOString(),positions,cash:amount($('#portfolio-cash').value,'Cash balance')};
}

const money=new Intl.NumberFormat(undefined,{style:'currency',currency:'USD',maximumFractionDigits:2});
const pct=new Intl.NumberFormat(undefined,{style:'percent',maximumFractionDigits:1});
function renderAllocation(snapshot) {
  const rows=$('#allocation-rows');rows.replaceChildren();
  const cash=Number(snapshot?.cash||0), positions=snapshot?.positions||[], unsupported=new Set(snapshot?.unsupported||[]);
  const omitted=positions.filter(position=>unsupported.has(position.symbol)||position.price===null||position.price===undefined||position.quantity===null||position.quantity===undefined);
  const valued=positions.filter(position=>!omitted.includes(position)).map(position=>({name:position.symbol,value:Number(position.quantity)*Number(position.price)})).filter(position=>Number.isFinite(position.value));
  const total=cash+valued.reduce((sum,position)=>sum+position.value,0);
  const omittedNames=[...new Set(omitted.map(position=>position.symbol))];
  const exclusion=omittedNames.length?`${omittedNames.join(', ')} excluded`:'All holdings valued';
  $('#known-value').textContent=snapshot?money.format(total):'—';
  $('#cash-value').textContent=snapshot?money.format(cash):'—';
  $('#cash-share').textContent=snapshot&&total>0?`${pct.format(cash/total)} of valued assets${omittedNames.length?`; ${exclusion}`:''}`:'Share of valued assets';
  $('#value-coverage').textContent=snapshot?`${valued.length} of ${positions.length} holdings valued${omittedNames.length?`; ${exclusion}`:''}`:'Based on saved prices';
  $('#chart-total').textContent=snapshot?money.format(total):'—';
  const chart=$('#allocation-chart');
  if(!snapshot||total<=0){chart.style.background='';chart.setAttribute('aria-label','No known portfolio value to chart');$('#allocation-caption').textContent=snapshot?'Add prices and shares to see allocation.':'Save holdings to see your allocation.';rows.innerHTML='<tr><td colspan="3">No known allocation yet</td></tr>';$('#allocation-note').textContent=omittedNames.length?`${exclusion} from known value and allocation.`:'Prices are values you last saved, not live quotes.';return;}
  const parts=[...valued];if(cash>0)parts.push({name:'Cash',value:cash});parts.sort((a,b)=>b.value-a.value);
  const tableParts=parts.slice(0,7);if(parts.length>7)tableParts.push({name:`${parts.length-7} other positions`,value:parts.slice(7).reduce((sum,part)=>sum+part.value,0)});
  const colors=['#184c44','#319982','#d69a57','#6b84a1','#b37664','#8e8e68','#a7bdad','#d8d1bd'];let offset=0;const stops=[];
  tableParts.forEach((part,index)=>{const angle=part.value/total*360;stops.push(`${colors[index]} ${offset}deg ${offset+angle}deg`);offset+=angle;const tr=document.createElement('tr');const name=document.createElement('th');name.scope='row';name.textContent=part.name;name.style.setProperty('--slice-color',colors[index]);const value=document.createElement('td');value.textContent=money.format(part.value);const share=document.createElement('td');share.textContent=`${pct.format(part.value/total)} of valued assets`;tr.append(name,value,share);rows.append(tr);});
  chart.style.background=`conic-gradient(${stops.join(',')})`;
  chart.setAttribute('aria-label',`Known valued assets ${money.format(total)}. ${tableParts.map(part=>`${part.name} ${pct.format(part.value/total)} of valued assets`).join('; ')}. ${omittedNames.length?exclusion+'.':''}`);
  $('#allocation-caption').textContent=`${valued.length} of ${positions.length} holdings valued`;
  $('#allocation-note').textContent=omittedNames.length?`${exclusion} from known value and allocation. Prices are the values you last saved.`:'Prices are the values you last saved, not live quotes.';
}

function updateResearchReadiness() {
  if(!state)return;
  const settings=state.settings, tests=state.connection_tests||{}, hasPortfolio=Boolean(state.snapshot);
  const searchConfigured=settings.search_provider==='keyless'||(settings.search_provider==='tavily'&&state.secrets_present.tavily_api_key);
  const modelConfigured=settings.model_enabled&&state.secrets_present.model_api_key;
  $('#cap-measures').textContent=hasPortfolio?'Ready from saved holdings and cash':'Add holdings or cash';
  $('#cap-events').textContent=!searchConfigured?'Unavailable · set up search':tests.search?.status==='passed'?`Search tested ${dateLabel(tests.search.tested_at)}`:tests.search?.status==='failed'?'Search test failed · retry':'Search configured · test it';
  $('#cap-model').textContent=!modelConfigured?'Unavailable · add a model':tests.model?.status==='passed'?`Model tested ${dateLabel(tests.model.tested_at)}`:tests.model?.status==='failed'?'Model test failed · retry':'Model configured · test it';
  $('#model-test-state').textContent=tests.model?.status==='passed'?`Last test passed ${dateLabel(tests.model.tested_at)}`:tests.model?.status==='failed'?`Last test failed ${dateLabel(tests.model.tested_at)}`:'Connection not tested';
  $('#search-test-state').textContent=tests.search?.status==='passed'?`Last test passed ${dateLabel(tests.search.tested_at)}`:tests.search?.status==='failed'?`Last test failed ${dateLabel(tests.search.tested_at)}`:'Connection not tested';
  $('#tavily-fields').hidden=settings.search_provider!=='tavily';
  const question=$('#run-form').elements.question.value;
  const asksCurrent=/\b(recent|latest|today|now|news|events?|earnings|filings?)\b/i.test(question);
  const wantsIdeas=$('#run-form').elements.include_suggestions.checked;
  const eventGap=asksCurrent&&!(searchConfigured&&modelConfigured&&tests.search?.status==='passed'&&tests.model?.status==='passed');
  const ideaGap=wantsIdeas&&(!settings.investment_profile||!modelConfigured);
  const warning=$('#question-readiness');warning.hidden=!eventGap&&!ideaGap;
  if(!warning.hidden)warning.textContent=[eventGap?'A recent-events answer needs current-event search and a tested model. This run may show portfolio measures without answering that question.':'',ideaGap?'Adjustment ideas need a complete investment profile and a working model.':''].filter(Boolean).join(' ');
}

function setMailProvider(preserve=false) {
  const provider=$('#mail-provider').value, form=$('#email-form');
  if(!preserve){
    if(provider==='gmail'){form.elements.host.value='smtp.gmail.com';form.elements.port.value='465';form.elements.tls_mode.value='implicit';}
    if(provider==='icloud'){form.elements.host.value='smtp.mail.me.com';form.elements.port.value='587';form.elements.tls_mode.value='starttls';}
  }
  $('#manual-smtp').open=provider==='custom';
  const help=$('#mail-provider-help');
  if(provider==='gmail')help.innerHTML='Use your full Gmail address and a Google app password if your account allows one. <a href="https://support.google.com/accounts/answer/185833" target="_blank" rel="noopener noreferrer">Google app-password instructions</a>.';
  else if(provider==='icloud')help.innerHTML='Use your full iCloud Mail address and an app-specific password. <a href="https://support.apple.com/en-us/102525" target="_blank" rel="noopener noreferrer">Apple mail settings</a>.';
  else help.innerHTML='Enter the SMTP details from your mail provider. Outlook.com requires OAuth2, which this password-based setup does not support. <a href="https://support.microsoft.com/en-us/outlook/pop-imap-and-smtp-settings-for-outlook-com" target="_blank" rel="noopener noreferrer">Outlook.com requirements</a>.';
}

async function refresh(populate=false) {
  state=await api('/api/status');
  const s=state.settings, snapshot=state.snapshot;
  if(!portfolioDirty && JSON.stringify(snapshot)!==editorVersion) loadPortfolio(snapshot);
  renderAllocation(snapshot);
  $('#snapshot-time').textContent=snapshot ? `Saved ${dateLabel(snapshot.as_of)} · ${snapshot.positions.length} holdings` : 'Add holdings manually, import a CSV, or try a screenshot.';
  $('#source-label').textContent=snapshot ? 'LAST SAVED PORTFOLIO' : 'NO PORTFOLIO SAVED';
  $('#top-status').textContent=snapshot ? `Positions as of ${dateLabel(snapshot.as_of)}` : 'Add positions to begin';
  $('#next-run').textContent=state.next_run ? `Next scheduled run: ${dateLabel(state.next_run)}` : 'Research available on demand';
  const latest=state.history[0]; $('#latest-status').textContent=latest ? latest.summary||({queued:'Queued',running:'Research in progress',failed:'Run failed'}[latest.status]||'Report ready') : 'No reports yet';
  const active=state.history.some(row => ['queued','running'].includes(row.status));
  $('#run-button').disabled=active; $('#run-button').textContent=active ? 'Research in progress…' : 'Run research →';
  $('#run-email-wrap').hidden=!(s.smtp && state.verified_recipient === s.smtp.recipient);
  $('#email-state').textContent=s.smtp ? (state.verified_recipient===s.smtp.recipient ? `Verified: ${s.smtp.recipient}` : 'Next: send a verification email and enter the code.') : 'First save your mail server settings.';
  $('#research-connection-status').textContent='Connection tests above show whether current-event and written research can be checked before a run.';
  updateResearchReadiness();
  $('#logout').hidden=!state.server;
  if (populate) {
    setForm('#model-form',{...s.model,enabled:s.model_enabled});
    setForm('#research-form',s); setForm('#preferences-form',s); setForm('#email-form',s.smtp);
    setForm('#schedule-form',{...s.schedule,scheduled_email:s.scheduled_email});
    if (s.investment_profile) {const targets={...s.investment_profile.allocation_targets};for(const name of ['stocks','bonds','cash']){const input=$('#profile-form').elements.namedItem(`target_${name}`);input.value=targets[name]??'';delete targets[name];}setForm('#profile-form',{...s.investment_profile,allocation_targets:Object.keys(targets).length?JSON.stringify(targets,null,2):'', restrictions:(s.investment_profile.restrictions||[]).join('\n')});}
    if(snapshot) $('#run-source').value='stored';
    const mail=s.smtp;const mailProvider=mail?.host==='smtp.gmail.com'?'gmail':mail?.host==='smtp.mail.me.com'?'icloud':mail?'custom':'gmail';$('#mail-provider').value=mailProvider;setMailProvider(true);
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
  renderSecretFields();
  const history=$('#history'); history.replaceChildren(); history.classList.toggle('empty',!state.history.length);
  if(!state.history.length) history.textContent='No research yet. Run a briefing above when ready.';
  for(const row of state.history) {
    const item=document.createElement('div'); item.className='history-row';
    const info=document.createElement('div'), title=document.createElement('strong'), detail=document.createElement('small');
    title.textContent=dateLabel(row.created_at); detail.textContent=row.error || (row.schedule_key ? `Scheduled session ${row.schedule_key}` : 'On-demand research');
    if(row.delivery) detail.textContent+=` · Email: ${row.delivery}`;
    info.append(title,detail);
    const actions=document.createElement('div'); actions.className='history-actions';
    const badge=document.createElement('span'); badge.className=`badge ${row.status}`; badge.textContent=({queued:'Queued',running:'Running',failed:'Failed',partial:'Review coverage',success:'Ready'})[row.status]||row.status; actions.append(badge);
    if(row.summary&&!row.error)detail.textContent=row.summary;
    if(['queued','running'].includes(row.status)) actions.append(button('Cancel', async()=>{await api(`/api/runs/${row.id}/cancel`,{}); await refresh();}));
    else if(row.status!=='failed') actions.append(button('Read report →',()=>openReport(row.id,true)));
    item.append(info,actions); history.append(item);
  }
  if(latest && ['success','partial'].includes(latest.status) && latest.id!==lastAutoReport && !selectedReport) {openReport(latest.id); lastAutoReport=latest.id;}
  if(pendingRunId){const pending=state.history.find(row=>row.id===pendingRunId);if(pending&&['success','partial','failed'].includes(pending.status)){pendingRunId=null;notice(pending.status==='failed'?'Research failed. Check the run in history.':'Research finished. Conclusions and coverage are shown above.',pending.status==='failed');if(pending.status!=='failed')await openReport(pending.id,true);}}
}
async function save(body) {for(const key of state.externally_managed) delete body[key];if(!Object.keys(body).length)throw new Error('These settings are managed by your deployment environment.');await api('/api/settings',body); await refresh(); notice('Settings saved.'); }
async function secret(name,value) { if(value) await api(`/api/secrets/${name}`,{value}); }
function renderSecretFields(){
  for(const [formId,field,key] of [['#model-form','key','model_api_key'],['#research-form','key','tavily_api_key'],['#research-form','alpha_key','alpha_vantage_api_key'],['#email-form','password','smtp_password']]){
    const input=$(formId).elements.namedItem(field), label=input.closest('label');
    let status=label.querySelector('.saved-secret');
    if(!status){status=document.createElement('span');status.className='saved-secret';const mask=document.createElement('span');mask.className='saved-secret-mask';mask.textContent='••••••••';const button=document.createElement('button');button.type='button';button.className='text-button';button.textContent='Change secret';button.addEventListener('click',()=>{input.hidden=false;input.dataset.editing='true';input.placeholder='Enter replacement secret';input.focus();});status.append('Secret saved ',mask,button);label.append(status);}
    const saved=Boolean(state.secrets_present[key]), editable=state.secrets_editable?.[key]!==false;
    status.hidden=!saved&&editable;status.firstChild.textContent=saved?(editable?'Secret saved ':'Managed secret '):'Set secret in deployment settings ';
    status.querySelector('.saved-secret-mask').hidden=!saved;
    status.querySelector('button').hidden=!editable;
    if(!saved&&editable){input.hidden=false;input.placeholder='Enter secret';}
    else if(input.dataset.editing!=='true'||!editable){input.value='';input.hidden=true;input.placeholder='';}
  }
}
if($('#login-form')) {
  $('#login-form').addEventListener('submit',action(async()=>{await api('/api/login',values('#login-form')); window.location.reload();}));
} else {
  $('#run-form').elements.question.closest('label').after($('#question-readiness'));
  $('#run-form').elements.question.addEventListener('input',updateResearchReadiness);
  $('#run-form').elements.include_suggestions.addEventListener('change',updateResearchReadiness);
  $('.research-card [data-view="connections"]').addEventListener('click',()=>{$('#research-setup').open=true;$('#research-setup').scrollIntoView({behavior:'smooth'});});
  $('#refresh').addEventListener('click',action(()=>refresh()));
  $('#logout').addEventListener('click',action(async()=>{await api('/api/logout',{});window.location.reload();}));
  $('#run-form').addEventListener('submit',action(async()=>{
    if(portfolioDirty) {view('desk');$('#portfolio-editor').scrollIntoView({behavior:'smooth'});throw new Error('Save or discard your portfolio edits before starting a briefing.');}
    const data=values('#run-form'); data.include_suggestions=checked('#run-form','include_suggestions'); data.send_email=checked('#run-form','send_email');
    const run=await api('/api/runs',data);pendingRunId=run.run_id;selectedReport=null;$('#run-form').elements.include_suggestions.checked=false;
    notice('Research started. Your report will appear here.'); await refresh();
  }));
  $('#import-form').addEventListener('submit',action(async()=>{
    const form=$('#import-form'); const file=form.elements.file.files[0]; if(!file || file.size>500000) throw new Error('Choose a CSV smaller than 500 KB.');
    importDraft={csv:await file.text(),as_of:new Date(form.elements.as_of.value).toISOString(),confirm:false};
    const preview=await api('/api/import',importDraft); $('#import-preview').hidden=false;
    loadPortfolio(preview,true); const missing=preview.positions.filter(p=>p.quantity===null||p.quantity===undefined||p.quantity==='').length;$('#import-preview').textContent=`${preview.positions.length} holdings in the draft. ${missing} ${missing===1?'needs':'need'} shares before saving. Check the cash balance too.`; $('#confirm-import').hidden=false; view('desk'); $('#portfolio-editor').scrollIntoView({behavior:'smooth'});focusFirstMissing();
  }));
  $('#confirm-import').addEventListener('click',()=>{view('desk');$('#portfolio-editor').scrollIntoView({behavior:'smooth'});focusFirstMissing();});
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
      view('desk'); $('#portfolio-editor').scrollIntoView({behavior:'smooth'});focusFirstMissing();
      notice((notes||[]).join('\n') || 'Review every extracted value, fill in missing quantities and cash, then save.');
    } finally {control.disabled=false;}
  }));
  $('#add-holding').addEventListener('click',()=>{addHolding();markPortfolioDirty();});
  $('#fill-details').addEventListener('click',action(async()=>{
    const rows=[...$('#holdings-rows').children];if(!rows.length)throw new Error('Add a ticker or import holdings first.');
    if(rows.length>30)throw new Error('Fill details supports up to 30 holdings at a time. Review a smaller draft.');
    const positions=rows.map(row=>{const item={};for(const input of row.querySelectorAll('input,select'))item[input.dataset.field]=input.value.trim();item.symbol=item.symbol.toUpperCase();for(const field of ['quantity','price','previous_close'])item[field]=item[field]?amount(item[field],`${item.symbol} ${field}`):null;for(const field of ['instrument_name','exchange','sector'])item[field]=item[field]||null;return item;});
    const control=$('#fill-details');control.disabled=true;notice('Looking up missing ticker details and daily-close prices. Your draft will remain unsaved.');
    try{const result=await api('/api/portfolio/enrich',{positions});result.positions.forEach((position,index)=>{const row=rows[index];for(const [field,value] of Object.entries(position)){const input=[...row.querySelectorAll('input,select')].find(element=>element.dataset.field===field);if(input&&value!==null&&value!==undefined)input.value=value;}});markPortfolioDirty();const names=result.changes.map(item=>`${item.symbol}: ${item.fields.join(', ')}${item.price_as_of?` (daily close ${item.price_as_of})`:''}${item.source?` via ${item.source}`:''}`);notice(names.length?`Added ${names.join('; ')}. Review and save your portfolio. Quantity is never inferred.`:'No verified missing details were available. Review ticker symbols or enter values manually.');}
    finally{control.disabled=false;}
  }));
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
  $('#model-form').addEventListener('submit',action(async()=>{const data=values('#model-form');await secret('model_api_key',data.key);$('#model-form').elements.key.value='';$('#model-form').elements.key.dataset.editing='false';await save({model:{provider:data.provider,model:data.model,base_url:data.base_url,temperature:Number(data.temperature),max_tokens:Number(data.max_tokens)},model_enabled:checked('#model-form','enabled')});}));
  $('#research-form').addEventListener('submit',action(async()=>{const data=values('#research-form');await secret('tavily_api_key',data.key);await secret('alpha_vantage_api_key',data.alpha_key);$('#research-form').elements.key.value='';$('#research-form').elements.key.dataset.editing='false';$('#research-form').elements.alpha_key.value='';$('#research-form').elements.alpha_key.dataset.editing='false';await save({search_provider:data.search_provider,sec_contact:data.sec_contact,primary_feeds_enabled:checked('#research-form','primary_feeds_enabled'),market_data_enabled:checked('#research-form','market_data_enabled'),alpha_vantage_enabled:checked('#research-form','alpha_vantage_enabled')});}));
  document.querySelectorAll('[data-test]').forEach(el=>el.addEventListener('click',action(async()=>{el.disabled=true;notice('Testing the saved connection…');try{const result=await api(`/api/test/${el.dataset.test}`,{});notice(result.message);}finally{el.disabled=false;await refresh();}})));
  $('#research-form').elements.search_provider.addEventListener('change',()=>{$('#tavily-fields').hidden=$('#research-form').elements.search_provider.value!=='tavily';});
  $('#preferences-form').addEventListener('submit',action(async()=>{const data=values('#preferences-form');for(const key of ['position_review_threshold_pct','sector_review_threshold_pct','max_searches','max_seconds','max_data_age_hours','retention_days']) data[key]=Number(data[key]);await save(data);}));
  $('#profile-form').addEventListener('submit',action(async()=>{const data=values('#profile-form');let targets={};try{targets=data.allocation_targets.trim()?JSON.parse(data.allocation_targets):{};}catch{throw new Error('Other targets must be a JSON object, for example {"real_estate":10}.');}if(!targets||Array.isArray(targets)||typeof targets!=='object')throw new Error('Other targets must be a JSON object.');for(const name of ['stocks','bonds','cash']){const raw=data[`target_${name}`].trim();delete data[`target_${name}`];if(raw)targets[name]=Number(raw);}if(!Object.keys(targets).length||Object.values(targets).some(value=>!Number.isFinite(value)||value<0||value>100)||Object.values(targets).reduce((sum,value)=>sum+value,0)>100)throw new Error('Enter targets between 0% and 100% that total no more than 100%.');data.allocation_targets=targets;data.restrictions=data.restrictions.split('\n').map(s=>s.trim()).filter(Boolean);await save({investment_profile:data});}));
  $('#mail-provider').addEventListener('change',()=>setMailProvider());
  $('#email-form').addEventListener('submit',action(async()=>{const data=values('#email-form');const provider=data.mail_provider;delete data.mail_provider;if(provider!=='custom'&&!data.username)data.username=data.sender;await secret('smtp_password',data.password);$('#email-form').elements.password.value='';$('#email-form').elements.password.dataset.editing='false';delete data.password;data.port=Number(data.port);data.plain_text_only=checked('#email-form','plain_text_only');data.percent_only=checked('#email-form','percent_only');await save({smtp:data,scheduled_email:false});}));
  $('#request-code').addEventListener('click',action(async()=>{const result=await api('/api/email/verification',{});notice(result.message,result.status!=='sent');}));
  $('#verify-form').addEventListener('submit',action(async()=>{await api('/api/email/verify',values('#verify-form'));await refresh();notice('Your inbox is verified. Delivery is now available when you choose it.');}));
  $('#schedule-form').addEventListener('submit',action(async()=>{const data=values('#schedule-form');await save({schedule:{enabled:checked('#schedule-form','enabled'),delay_minutes:Number(data.delay_minutes),timezone:data.timezone},scheduled_email:checked('#schedule-form','scheduled_email')});}));
  $('#send-report').addEventListener('click',action(async()=>{const result=await api(`/api/runs/${selectedReport}/send`,{});notice(result.message,result.status!=='sent');await refresh();}));
  refresh(true).catch(error=>notice(error.message,true));
  setInterval(()=>refresh().catch(error=>notice(error.message,true)),4000);
}
