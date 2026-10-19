"use strict";
const $ = (selector) => document.querySelector(selector);
let state = null, importDraft = null, selectedReport = null, lastAutoReport = null, pendingRunId = null;
const csrf = $('meta[name="csrf-token"]').content;
function alertBox(selector) {
  const box = $(selector); if (!box) return null;
  if (!box.querySelector('.alert-text')) {
    const text = document.createElement('span'); text.className = 'alert-text';
    while (box.firstChild) text.append(box.firstChild);
    box.append(text);
    const close = document.createElement('button');
    close.type = 'button'; close.className = 'alert-close'; close.setAttribute('aria-label', 'Dismiss notification');
    close.textContent = '\u00d7';
    close.addEventListener('click', () => { box.hidden = true; });
    box.append(close);
  }
  return box;
}
function setAlertText(box, message) { const text = box && box.querySelector('.alert-text'); if (text) text.textContent = message; return text; }
function notice(message, error=false) {
  const box = alertBox('#notice'); if (!box) return;
  setAlertText(box, message);
  box.hidden = false; box.classList.toggle('error', error);
}
function clearFieldErrors() {
  document.querySelectorAll('.field-error').forEach(el => el.remove());
  document.querySelectorAll('[aria-invalid="true"]').forEach(el => {el.removeAttribute('aria-invalid');el.removeAttribute('aria-describedby');});
}
function fieldElement(field) {
  const parts = String(field || '').split('.').filter(part => part && part !== 'body' && !/^\d+$/.test(part));
  for (let index = parts.length - 1; index >= 0; index -= 1) {
    const key = parts[index];
    for (const el of document.querySelectorAll('[name]')) if (el.name === key) return el;
    for (const el of document.querySelectorAll('[data-field]')) if (el.dataset.field === key) return el;
  }
  return null;
}
function showFieldErrors(error) {
  clearFieldErrors();
  if (!error || !Array.isArray(error.errors) || !error.errors.length) return [];
  const shown = [];
  for (const item of error.errors.slice(0, 8)) {
    const el = item.field ? fieldElement(item.field) : null;
    const wrap = el && (el.closest('label') || el.parentElement);
    if (!wrap) continue;
    const note = document.createElement('span');
    note.className = 'field-error'; note.id = `field-error-${shown.length}`; note.setAttribute('role', 'alert');
    note.textContent = String(item.message || 'This value is not valid.');
    el.setAttribute('aria-invalid', 'true');
    el.setAttribute('aria-describedby', note.id);
    wrap.append(note);
    shown.push(note);
  }
  return shown;
}
async function api(path, body, method) {
  const response = await fetch(path, {method: method || (body === undefined ? 'GET' : 'POST'),
    headers: {'Content-Type':'application/json', 'X-CSRF-Token':csrf},
    body: body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(typeof data.detail === 'string' ? data.detail : 'Check the form fields and try again.');
    if (Array.isArray(data.errors)) error.errors = data.errors;  // field/message pairs; never the submitted value
    throw error;
  }
  return data;
}
function action(fn) { return async (event) => {
  if(event) event.preventDefault();
  try { await fn(event); }
  catch(error) {
    const inline = showFieldErrors(error);
    notice(inline.length ? inline.map(note => note.textContent).join(' ') : error.message, true);
  }
}; }
function values(id) { const form=$(id), data=Object.fromEntries(new FormData(form).entries()); for(const element of form.elements) {if(element.disabled && element.name && element.type!=='checkbox') data[element.name]=element.value;} return data; }
function checked(id, name) { return $(id).elements.namedItem(name).checked; }
function setForm(id, data) {
  const form = $(id);
  for (const [name, value] of Object.entries(data || {})) {
    const element = form.elements.namedItem(name); if (!element || element.type === 'password') continue;
    if (element.type === 'checkbox') element.checked = Boolean(value); else element.value = value ?? '';
  }
}
const VIEW_ROUTES={desk:'portfolio',portfolio:'portfolio',connections:'connections',preferences:'preferences',automation:'automation'};
const HASH_VIEWS={desk:'desk',portfolio:'desk',connections:'connections',preferences:'preferences',automation:'automation'};
function viewFromHash(hash) { const key=String(hash||'').replace(/^#/,'').split('?')[0].toLowerCase(); return HASH_VIEWS[key]||null; }
let currentView='desk';
function view(name, updateHash=true) {
  currentView=name;
  document.querySelectorAll('.view').forEach(el => {el.hidden = el.id !== `view-${name}`;});
  document.querySelectorAll('.nav').forEach(el => {const active=el.dataset.view===name;el.classList.toggle('active',active);if(active)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');});
  const route=VIEW_ROUTES[name];
  if(updateHash && route && window.location.hash !== `#${route}`) window.location.hash=route;
  window.scrollTo({top:0, behavior:'smooth'});
}
document.querySelectorAll('[data-view]').forEach(el => el.addEventListener('click', () => view(el.dataset.view)));
function dateLabel(value) { return value ? new Date(value).toLocaleString('en-US', {year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZone:state?.settings?.schedule?.timezone||'America/New_York',timeZoneName:'short'}) : 'On demand'; }
function button(label, callback, className='text-button') { const el=document.createElement('button'); el.textContent=label; el.className=className; el.addEventListener('click',action(callback)); return el; }
function sizeReportFrame() {
  // The embedded report is self-contained and same-origin, so match its height and
  // let the page scroll instead of nesting a second scrollbar inside the card.
  const frame=$('#report-frame'); if(!frame)return;
  try {
    const doc=frame.contentDocument; if(!doc)return;
    const height=Math.max(doc.body?doc.body.scrollHeight:0,doc.documentElement?doc.documentElement.scrollHeight:0);
    if(height>0) frame.style.height=`${height}px`;
  } catch { /* unreadable document: keep the CSS min-height fallback */ }
}
async function openReport(id, scroll=false) {
  selectedReport=id; const encoded=encodeURIComponent(id);
  const frame=$('#report-frame'); frame.style.height=''; frame.src=`/reports/${encoded}?embed=true`;
  $('#report-open').href=`/reports/${encoded}`;
  $('#report-text').href=`/reports/${encoded}?plain=true`;
  $('#report-card').hidden=false;
  $('#send-report').hidden=!(state.settings.smtp && state.verified_recipient === state.settings.smtp.recipient);
  $('#report-summary').textContent='Loading conclusions…';
  try { const row=await api(`/api/runs/${encoded}`); if(selectedReport===id) {renderReportSummary(row);renderResearchActivity(row);} if(scroll) $('#report-card').scrollIntoView({behavior:'smooth',block:'start'}); }
  catch(error) { if(selectedReport===id) $('#report-summary').textContent=`Could not load the summary: ${error.message}`; }
}

const NEWS_STATUS={verified:{label:'verified against the source',dot:'verified'},no_verified_update:{label:'no verified recent news',dot:'quiet'},discovery_only:{label:'headline only (not verified)',dot:'headline'},search_failed:{label:'news search failed',dot:'failed'},not_checked:{label:'not checked in this run',dot:'unchecked'}};
function renderNewsLegend(news) {
  const list=document.createElement('ul');list.className='news-legend';
  for(const item of news.slice(0,20)){
    const meta=NEWS_STATUS[item.status]||{label:'checked',dot:'quiet'};
    const li=document.createElement('li');
    const dot=document.createElement('span');dot.className=`news-dot ${meta.dot}`;dot.setAttribute('aria-hidden','true');
    const symbol=document.createElement('strong');symbol.textContent=item.symbol||'Holding';
    const label=document.createElement('span');
    label.textContent=item.status==='verified'?(meta.label+(item.evidence_grade==='headline'?' · headline, not full text':item.evidence_grade==='full'?' · full-text source':'')):meta.label;
    li.append(dot,symbol,label);list.append(li);
  }
  return list;
}
function renderReportSummary(row) {
  const root=$('#report-summary');root.replaceChildren();
  const result=row.result;if(!result){root.textContent='This report is still running.';$('#report-coverage').textContent='';return;}
  $('#report-date').textContent=`Positions as of ${dateLabel(result.snapshot_as_of)}`;
  const coverage=result.coverage||{};
  $('#report-coverage').textContent=coverage.summary?`Research coverage: ${coverage.summary}`:'';
  const summaryTitle=document.createElement('h3');summaryTitle.textContent='Briefing summary';
  const summary=document.createElement('p');summary.className='briefing-summary';summary.textContent=briefingSummary(result);root.append(summaryTitle,summary);
  const summaryLinks=document.createElement('div');summaryLinks.className='insight-sources';
  for(const id of result.executive_summary?.evidence_ids||[]){const source=(result.sources||[]).find(item=>item.id===id);if(!source)continue;try{const url=new URL(source.url);if(!['http:','https:'].includes(url.protocol)||url.username)continue;const link=document.createElement('a');link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';link.textContent=source.title||source.publisher||'Source';summaryLinks.append(link);}catch{}}
  if(summaryLinks.childElementCount)root.append(summaryLinks);
  const findings=(result.findings||[]).filter(item=>item.title!=='Portfolio overview').sort((a,b)=>Number(Boolean(b.evidence_ids?.length))-Number(Boolean(a.evidence_ids?.length))).slice(0,4);
  const matter=document.createElement('h3');matter.textContent='What matters now';root.append(matter);
  if(!findings.length){const empty=document.createElement('p');empty.className='insight-context';empty.textContent='No supported conclusion is available for this run. See what we could check below.';root.append(empty);}
  const sources=new Map((result.sources||[]).map(source=>[source.id,source]));
  const list=document.createElement('div');list.className='insight-list';
  findings.forEach((finding,index)=>{const article=document.createElement('article');article.className='insight';const number=document.createElement('span');number.className='insight-number';number.textContent=String(index+1).padStart(2,'0');const body=document.createElement('div');const title=document.createElement('h3');title.textContent=finding.title;const detail=document.createElement('p');detail.textContent=finding.detail;body.append(title,detail);const context=[finding.event_date?`Event: ${finding.event_date}`:'',finding.affected_symbols?.length?`Affects: ${finding.affected_symbols.join(', ')}`:'',finding.portfolio_relevance||'',finding.uncertainty?`Limit: ${finding.uncertainty}`:''].filter(Boolean);for(const note of context){const p=document.createElement('p');p.className='insight-context';p.textContent=note;body.append(p);}const links=document.createElement('div');links.className='insight-sources';const ids=(finding.evidence_ids||[]).filter(id=>sources.has(id));ids.forEach((id,i)=>{const source=sources.get(id);try{const url=new URL(source.url);if(!['http:','https:'].includes(url.protocol))return;const anchor=document.createElement('a');anchor.href=url.href;anchor.target='_blank';anchor.rel='noopener noreferrer';anchor.textContent=source.title||source.publisher||`Source ${i+1}`;links.append(anchor);}catch{}});if(links.childElementCount)body.append(links);article.append(number,body);list.append(article);});
  root.append(list);
  const news=coverage.position_news||(result.metrics&&result.metrics.position_news)||[];
  if(news.length){const newsTitle=document.createElement('h3');newsTitle.textContent='What we could check for each holding';root.append(newsTitle);root.append(renderNewsLegend(news));}
  const issues=(coverage.issues||[]).filter(issue=>issue&&issue.message);
  const nextTitle=document.createElement('h3');nextTitle.textContent='Data notes';root.append(nextTitle);
  if(issues.length){const issueList=document.createElement('ul');issueList.className='coverage-issues';for(const issue of issues.slice(0,2)){const li=document.createElement('li');li.append(document.createTextNode(issue.message+' '));if(issue.action){const label=issue.action==='portfolio'?'Fix holdings':issue.action==='model'?'Set up a model':'Set up research';const go=button(label,()=>goToIssue(issue.action));go.type='button';li.append(go);}issueList.append(li);}root.append(issueList);if(issues.length>2){const extra=document.createElement('details');const summary=document.createElement('summary');summary.textContent=`${issues.length-2} more data notes`;extra.append(summary);for(const issue of issues.slice(2)){const note=document.createElement('p');note.textContent=issue.message;extra.append(note);}root.append(extra);}}
  else {const positive=document.createElement('p');positive.className='insight-context';positive.textContent='No additional data notes were recorded for this run.';root.append(positive);}
}

function briefingSummary(result) {
  if(result.executive_summary?.text)return result.executive_summary.text;
  const finding=(result.findings||[]).find(item=>item.evidence_ids?.length&&item.title!=='Portfolio overview');
  if(finding)return finding.detail||finding.title;
  const weights=Object.entries(result.metrics?.weights_pct||{}).filter(([,value])=>Number.isFinite(Number(value))).sort((a,b)=>Number(b[1])-Number(a[1]));
  if(weights.length)return `${weights[0][0]} represents ${Number(weights[0][1]).toFixed(1)}% of the holdings and cash we could value. This run did not establish a sourced current-event conclusion.`;
  return 'This run did not establish a supported current-event conclusion. Review research coverage and data notes for unchecked sources or incomplete research.';
}

function renderResearchActivity(row) {
  const card=$('#research-activity');if(!card)return;
  const events=row?.research_activity||row?.result?.research_activity||[];
  const budget=row?.research_budget||row?.result?.research_budget||{};
  card.hidden=!row;if(!row)return;
  $('#activity-status').textContent=`Run ${row.id?.slice(0,8)||''} · ${row.status||'complete'}`;
  const labels={model_calls:'Model calls',tool_calls:'Tool calls',searches:'Discovery queries',elapsed_seconds:'Elapsed seconds',max_model_calls:'Model call limit',max_tool_calls:'Tool call limit',max_searches:'Query limit',max_seconds:'Time limit (seconds)'};
  $('#activity-budget').textContent=Object.entries(budget).filter(([,value])=>typeof value==='number').map(([key,value])=>`${labels[key]||key.replaceAll('_',' ')}: ${Math.round(value*10)/10}`).join(' · ');
  const list=$('#activity-entries');list.replaceChildren();
  if(!events.length){const entry=document.createElement('li');entry.textContent=['queued','running'].includes(row.status)?'Waiting for the first research tool result…':'No tool activity was recorded for this report.';list.append(entry);}
  for(const event of events){const entry=document.createElement('li');const title=document.createElement('strong');title.textContent=event.tool||'Research tool';const detail=document.createElement('span');const seconds=event.elapsed_seconds??(event.elapsed_ms===undefined?null:event.elapsed_ms/1000);detail.textContent=[event.asset||event.symbol,event.query,event.source,event.status,seconds===null?null:`${Number(seconds).toFixed(1)}s`].filter(Boolean).join(' · ');entry.append(title,detail);list.append(entry);}
}

function goToIssue(actionName){if(actionName==='portfolio'){view('desk');$('#portfolio-editor').scrollIntoView({behavior:'smooth'});return;}view('connections');$('#research-setup').open=true;$('#research-setup').scrollIntoView({behavior:'smooth'});}

let portfolioDirty = false, editorSnapshot = null, editorVersion = null, activeImportSource = null;
let pricesBySymbol = new Map(), pricesAsOf = null;
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
  for(const [name,label] of [['symbol','Ticker'],['quantity','Shares']]) {
    const cell=document.createElement('td'), input=document.createElement('input');cell.dataset.label=label;
    input.dataset.field=name; input.setAttribute('aria-label',label); input.value=position[name] ?? '';
    if(name==='quantity') input.inputMode='decimal';
    input.required=true;
    input.addEventListener('input',markPortfolioDirty);cell.append(input);row.append(cell);
  }
  const priceCell=document.createElement('td');priceCell.dataset.label='Price';
  const priceText=document.createElement('span');priceText.className='holding-price';
  const priceValue=position.price ?? pricesBySymbol.get(position.symbol)?.price;
  priceText.textContent=formatPrice(priceValue);
  priceText.title='Price updates automatically from market data. Enter ticker and shares.';
  priceText.setAttribute('aria-label',`Price for ${position.symbol || 'holding'}: ${formatPrice(priceValue)}`);
  priceCell.append(priceText);row.append(priceCell);
  if(position.symbol && priceValue!==null && priceValue!==undefined && priceValue!=='') pricesBySymbol.set(position.symbol.toUpperCase(),{price:priceValue,previous_close:position.previous_close ?? pricesBySymbol.get(position.symbol)?.previous_close});
  const symbolCell=row.firstElementChild;const details=document.createElement('details');details.className='instrument-details';const summary=document.createElement('summary');summary.textContent='Instrument details';details.append(summary);
  const typeLabel=document.createElement('label');typeLabel.textContent='Type';const type=document.createElement('select');type.dataset.field='asset_type';type.setAttribute('aria-label','Instrument type');for(const [value,text] of [['unknown','Unknown'],['stock','Stock'],['etf','ETF'],['fund','Fund'],['mutual_fund','Mutual fund'],['bond','Bond'],['cash','Cash'],['other','Other']]){const option=document.createElement('option');option.value=value;option.textContent=text;type.append(option);}type.value=position.asset_type || 'unknown';if(!type.value){const option=document.createElement('option');option.value=position.asset_type;option.textContent=position.asset_type;type.append(option);type.value=position.asset_type;}type.addEventListener('change',markPortfolioDirty);typeLabel.append(type);details.append(typeLabel);
  for(const [name,label] of [['instrument_name','Instrument name'],['exchange','Exchange'],['sector','Sector']]){const fieldLabel=document.createElement('label');fieldLabel.textContent=label;const input=document.createElement('input');input.dataset.field=name;input.value=position[name]??'';input.addEventListener('input',markPortfolioDirty);fieldLabel.append(input);details.append(fieldLabel);}symbolCell.append(details);
  const cell=document.createElement('td');cell.dataset.label='Action'; const remove=button('Remove',()=>{row.remove();markPortfolioDirty();});remove.type='button';remove.setAttribute('aria-label',`Remove ${position.symbol || 'holding'}`);cell.append(remove);row.append(cell);$('#holdings-rows').append(row);
}
function priceValue(value){ if(value===null||value===undefined||value==='')return null; return String(value).trim().replace(/^\$\s*/,'').replaceAll(',','') || null; }
function formatPrice(value){ const numeric=Number(priceValue(value)); return value===null||value===undefined||value===''||!Number.isFinite(numeric)?'—':money.format(numeric); }
function editableProjection(snapshot){
  if(!snapshot) return 'null';
  const positions=(snapshot.positions||[]).map(position=>({symbol:position.symbol,quantity:position.quantity,asset_type:position.asset_type,instrument_name:position.instrument_name,exchange:position.exchange,sector:position.sector}));
  return JSON.stringify({as_of:snapshot.as_of,cash:snapshot.cash,source:snapshot.source,account_id:snapshot.account_id,positions});
}
function relativeTime(value){
  if(!value)return 'not yet';
  const seconds=Math.max(0,(Date.now()-new Date(value).getTime())/1000);
  if(seconds<60)return 'just now';
  const minutes=Math.floor(seconds/60);if(minutes<60)return `${minutes} min ago`;
  const hours=Math.floor(minutes/60);if(hours<24)return `${hours} h ago`;
  return dateLabel(value);
}
function priceNote(){ return `Prices update automatically; last updated ${pricesAsOf?relativeTime(pricesAsOf):'when market data arrives'}.`; }
function updatePricesLabel(){ const label=$('#prices-updated'); if(label) label.textContent=pricesAsOf?`Prices updated ${relativeTime(pricesAsOf)}`:'Prices update automatically'; }
function applyQuotes(prices){
  const update=(positions)=>{for(const position of positions||[]){const quote=prices[position.symbol];if(quote&&quote.price!==null&&quote.price!==undefined){position.price=quote.price;if(quote.previous_close!==null&&quote.previous_close!==undefined)position.previous_close=quote.previous_close;pricesBySymbol.set(position.symbol,{price:quote.price,previous_close:position.previous_close});}}};
  update(editorSnapshot?.positions);update(state?.snapshot?.positions);
  for(const row of $('#holdings-rows').children){
    const symbol=[...row.querySelectorAll('input,select')].find(field=>field.dataset.field==='symbol')?.value.trim().toUpperCase();
    const quote=prices[symbol];const cell=row.querySelector('.holding-price');
    if(cell&&quote)cell.textContent=formatPrice(quote.price);
  }
}
function loadPortfolio(snapshot, draft=false) {
  editorSnapshot=snapshot ? structuredClone(snapshot) : {as_of:new Date().toISOString(),positions:[],cash:'0',source:'manual',account_id:'manual',unsupported:[]};
  activeImportSource=draft?(snapshot?.source==='image'?'Screenshot':'CSV'):null;
  pricesBySymbol=new Map();
  for(const position of editorSnapshot.positions||[]) pricesBySymbol.set(position.symbol,{price:priceValue(position.price),previous_close:priceValue(position.previous_close)});
  $('#holdings-rows').replaceChildren();for(const position of editorSnapshot.positions) addHolding(position);
  $('#portfolio-date').value=localDate(editorSnapshot.as_of);$('#portfolio-cash').value=editorSnapshot.cash ?? '';
  portfolioDirty=draft; editorVersion=editableProjection(snapshot);
  const missing=editorSnapshot.positions.filter(p=>p.quantity===null||p.quantity===undefined||p.quantity==='').length;
  editStatus(draft?`Import draft: ${missing} ${missing===1?'holding needs':'holdings need'} shares. Check cash, then save.`:'Saved portfolio. Enter ticker and shares here.');
  updateDraftProgress();
}
function collectPortfolio() {
  const positions=[], seen=new Set();
  for(const row of $('#holdings-rows').children) {
    const data={};for(const input of row.querySelectorAll('input,select')) data[input.dataset.field]=input.value.trim();
    data.symbol=data.symbol.toUpperCase();
    if(!/^[A-Z0-9.\-^=/]{1,40}$/.test(data.symbol)) throw new Error('Provide a valid symbol for every holding.');
    if(seen.has(data.symbol) && !['CASH','USD'].includes(data.symbol) && data.asset_type.toLowerCase()!=='cash') throw new Error(`Duplicate symbol ${data.symbol}; combine lots first.`);
    seen.add(data.symbol);const saved=pricesBySymbol.get(data.symbol)||{};
    data.quantity=amount(data.quantity,`${data.symbol} shares`);data.price=priceValue(saved.price);data.previous_close=priceValue(saved.previous_close);
    data.sector=data.sector||null;data.asset_type=(data.asset_type||'unknown').toLowerCase();positions.push(data);
  }
  const date=new Date($('#portfolio-date').value);if(!Number.isFinite(date.getTime())) throw new Error('Provide a snapshot date and time.');
  return {...editorSnapshot,as_of:date.toISOString(),positions,cash:amount($('#portfolio-cash').value,'Cash balance')};
}

const money=new Intl.NumberFormat(undefined,{style:'currency',currency:'USD',maximumFractionDigits:2});
const compactMoney=new Intl.NumberFormat(undefined,{notation:'compact',style:'currency',currency:'USD',maximumFractionDigits:1});
const pct=new Intl.NumberFormat(undefined,{style:'percent',maximumFractionDigits:1});
function exactMoney(value){return money.format(Number(value));}
function compactFigure(value){return compactMoney.format(Number(value));}
// Compact only long strings so ordinary amounts stay exact in place; the exact
// figure is always kept for titles and accessible names.
function displayFigure(value){const exact=exactMoney(value);return exact.length>9?{text:compactFigure(value),exact}:{text:exact,exact};}
function renderAllocation(snapshot) {
  const rows=$('#allocation-rows');rows.replaceChildren();
  const cash=Number(snapshot?.cash||0), positions=snapshot?.positions||[], unsupported=new Set(snapshot?.unsupported||[]);
  const omitted=positions.filter(position=>unsupported.has(position.symbol)||position.price===null||position.price===undefined||position.quantity===null||position.quantity===undefined);
  const valued=positions.filter(position=>!omitted.includes(position)).map(position=>({name:position.symbol,value:Number(position.quantity)*Number(position.price)})).filter(position=>Number.isFinite(position.value));
  const total=cash+valued.reduce((sum,position)=>sum+position.value,0);
  const omittedNames=[...new Set(omitted.map(position=>position.symbol))];
  const exclusion=omittedNames.length?`${omittedNames.join(', ')} excluded`:'All holdings priced';
  const knownFigure=snapshot?displayFigure(total):{text:'—',exact:''};
  const cashFigure=snapshot?displayFigure(cash):{text:'—',exact:''};
  const known=$('#known-value');known.textContent=knownFigure.text;known.title=knownFigure.exact;known.setAttribute('aria-label',snapshot?`Holdings we could price and cash: ${knownFigure.exact}`:'No portfolio saved');
  const cashEl=$('#cash-value');cashEl.textContent=cashFigure.text;cashEl.title=cashFigure.exact;cashEl.setAttribute('aria-label',snapshot?`Cash balance: ${cashFigure.exact}`:'No cash balance saved');
  $('#cash-share').textContent=snapshot&&total>0?`${pct.format(cash/total)} of what we could price${omittedNames.length?`; ${exclusion}`:''}`:'Share of what we could price';
  $('#value-coverage').textContent=snapshot?`${valued.length} of ${positions.length} holdings we could price${omittedNames.length?`; ${exclusion}`:''}`:'Prices update automatically';
  const chartTotal=$('#chart-total');const chartFigure=snapshot?{text:compactFigure(total),exact:exactMoney(total)}:{text:'—',exact:''};
  chartTotal.textContent=chartFigure.text;chartTotal.title=chartFigure.exact;
  const chart=$('#allocation-chart');
  if(!snapshot||total<=0){chart.style.background='';chart.setAttribute('aria-label','No known portfolio value to chart');$('#allocation-caption').textContent=snapshot?'Add shares to see allocation.':'Save holdings to see your allocation.';rows.innerHTML='<tr><td colspan="3">No known allocation yet</td></tr>';$('#allocation-note').textContent=omittedNames.length?`${exclusion} from known value and allocation. ${priceNote()}`:priceNote();return;}
  const parts=[...valued];if(cash>0)parts.push({name:'Cash',value:cash});parts.sort((a,b)=>b.value-a.value);
  const tableParts=parts.slice(0,7);if(parts.length>7)tableParts.push({name:`${parts.length-7} other positions`,value:parts.slice(7).reduce((sum,part)=>sum+part.value,0)});
  const colors=['#184c44','#319982','#d69a57','#6b84a1','#b37664','#8e8e68','#a7bdad','#d8d1bd'];let offset=0;const stops=[];
  tableParts.forEach((part,index)=>{const angle=part.value/total*360;stops.push(`${colors[index]} ${offset}deg ${offset+angle}deg`);offset+=angle;const tr=document.createElement('tr');const name=document.createElement('th');name.scope='row';name.textContent=part.name;name.title=part.name;name.style.setProperty('--slice-color',colors[index]);const value=document.createElement('td');const partFigure=displayFigure(part.value);value.textContent=partFigure.text;value.title=partFigure.exact;const share=document.createElement('td');share.textContent=pct.format(part.value/total);share.title=`${pct.format(part.value/total)} of what we could price`;tr.append(name,value,share);rows.append(tr);});
  chart.style.background=`conic-gradient(${stops.join(',')})`;
  chart.setAttribute('aria-label',`Holdings we could price and cash ${exactMoney(total)}. ${tableParts.map(part=>`${part.name} ${pct.format(part.value/total)} of what we could price`).join('; ')}. ${omittedNames.length?exclusion+'.':''}`);
  $('#allocation-caption').textContent=`${valued.length} of ${positions.length} holdings we could price`;
  $('#allocation-note').textContent=omittedNames.length?`${exclusion} from known value and allocation. ${priceNote()}`:priceNote();
}

function updateResearchReadiness() {
  if(!state)return;
  const settings=state.settings, tests=state.connection_tests||{}, hasPortfolio=Boolean(state.snapshot);
  const searchConfigured=settings.search_provider==='keyless'||(settings.search_provider==='tavily'&&state.secrets_present.tavily_api_key)||(settings.search_provider!=='disabled'&&(settings.primary_feeds_enabled||settings.sec_company_filings_enabled||settings.ticker_news_enabled));
  const modelConfigured=settings.model_enabled&&state.secrets_present.model_api_key;
  const sourceTests=tests.search?.sources;
  const testedWorkingSources=Object.values(sourceTests||{}).filter(item=>['passed','empty'].includes(item.status)).length;
  $('#cap-measures').textContent=hasPortfolio?'Ready from saved holdings and cash':'Add holdings or cash';
  $('#cap-events').textContent=!searchConfigured?'Unavailable · set up search':testedWorkingSources?`${testedWorkingSources} sources available${tests.search.status==='partial'?' · some sources need attention':''}`:tests.search?.status==='failed'||tests.search?.status==='partial'?'No tested source available · review source checks':'Search configured · test it';
  $('#cap-model').textContent=!modelConfigured?'Unavailable · add a model':tests.model?.status==='passed'?`Model tested ${dateLabel(tests.model.tested_at)}`:tests.model?.status==='failed'?'Model test failed · retry':'Model configured · test it';
  $('#model-test-state').textContent=tests.model?.status==='passed'?`Last test passed ${dateLabel(tests.model.tested_at)}`:tests.model?.status==='failed'?`Last test failed ${dateLabel(tests.model.tested_at)}`:'Connection not tested';
  $('#search-test-state').textContent=sourceTests?`Source checks ${dateLabel(tests.search.tested_at)} · see individual outcomes`:tests.search?`Legacy aggregate test: ${tests.search.status}. Retest to check each source.`:'Sources not tested';
  const chips=$('#source-states');chips.replaceChildren();
  const enabled=settings.search_provider!=='disabled';
  const sourceSettings={search:enabled,fed:enabled&&settings.primary_feeds_enabled,sec_filings:enabled&&settings.sec_company_filings_enabled,google_news:enabled&&settings.ticker_news_enabled&&(settings.news_feed_sources||[]).includes('google_news'),nasdaq:enabled&&settings.ticker_news_enabled&&(settings.news_feed_sources||[]).includes('nasdaq'),bing_news:enabled&&settings.ticker_news_enabled&&(settings.news_feed_sources||[]).includes('bing_news')};
  const sourceLabels={search:settings.search_provider==='tavily'?'Tavily':'Web search',fed:'Federal Reserve',sec_filings:'SEC filings',google_news:'Google News',nasdaq:'Nasdaq',bing_news:'Bing News'};
  for(const [name,active] of Object.entries(sourceSettings)){
    const result=sourceTests?.[name];const chip=document.createElement('span');chip.className='badge';
    const status=result?.status||(!active?'disabled':name==='sec_filings'&&!settings.sec_contact?'skipped':'not tested');
    chip.textContent=`${sourceLabels[name]}: ${status}`;chip.title=result?.reason||'';
    if(result?.reason){const detail=document.createElement('small');detail.textContent=` ${result.reason}`;chip.append(detail);}
    chips.append(chip);
  }
  updateSecAvailability();
  $('#tavily-fields').hidden=settings.search_provider!=='tavily';
  const newsSources=$('#news-source-fields'); if(newsSources) newsSources.hidden=!settings.ticker_news_enabled;
  const question=$('#run-form').elements.question.value;
  const asksCurrent=/\b(recent|latest|today|now|news|events?|earnings|filings?)\b/i.test(question);
  const wantsIdeas=$('#run-form').elements.include_suggestions.checked;
  const eventGap=asksCurrent&&!(searchConfigured&&modelConfigured&&testedWorkingSources>0&&tests.model?.status==='passed');
  const ideaGap=wantsIdeas&&(!settings.investment_profile||!modelConfigured);
  const warning=$('#question-readiness');warning.hidden=!eventGap&&!ideaGap;
  if(!warning.hidden)warning.textContent=[eventGap?'A recent-events answer needs current-event search and a tested model. This run may show portfolio measures without answering that question.':'',ideaGap?'Adjustment ideas need a complete investment profile and a working model.':''].filter(Boolean).join(' ');
}

function updateSecAvailability(){
  const form=$('#research-form');if(!form)return;
  const contact=form.elements.sec_contact.value.trim();
  const hasContact=/[^\s@]+@[^\s@]+\.[^\s@]+/.test(contact);
  const on=form.elements.sec_company_filings_enabled.checked;
  $('#sec-availability').textContent=!hasContact?(on?'SEC filings will be skipped until an identifying contact (name + email) is added.':'SEC filings are off. Add an identifying contact (name + email) to enable this source.'):(on?'SEC filings are enabled with your identifying contact.':'SEC filings are off. Enable the checkbox to include them.');
}

function setMailProvider(preserve=false) {
  const provider=$('#mail-provider').value, form=$('#email-form');
  if(!preserve){
    if(provider==='gmail'){form.elements.host.value='smtp.gmail.com';form.elements.port.value='465';form.elements.tls_mode.value='implicit';}
    if(provider==='icloud'){form.elements.host.value='smtp.mail.me.com';form.elements.port.value='587';form.elements.tls_mode.value='starttls';}
  }
  $('#manual-smtp').open=provider==='custom';
  const help=$('#mail-provider-help');
  if(provider==='gmail')help.innerHTML='Use your full Gmail address and an app password. 2-Step Verification plus an app password is required (an app password is not your account password); a Google Workspace admin may need to allow SMTP AUTH. This form never requests OAuth scopes. <a href="https://knowledge.workspace.google.com/admin/gmail/send-email-from-a-printer-scanner-or-app" target="_blank" rel="noopener noreferrer">Google Workspace guide to sending mail from an app</a>. <a href="https://support.google.com/accounts/answer/185833" target="_blank" rel="noopener noreferrer">Google app-password instructions</a>.';
  else if(provider==='icloud')help.innerHTML='Use your full iCloud Mail address and an app-specific password. <a href="https://support.apple.com/en-us/102525" target="_blank" rel="noopener noreferrer">Apple mail settings</a>.';
  else help.innerHTML='Enter the SMTP details from your mail provider. Outlook.com requires OAuth2, which this password-based setup does not support. <a href="https://support.microsoft.com/en-us/outlook/pop-imap-and-smtp-settings-for-outlook-com" target="_blank" rel="noopener noreferrer">Outlook.com requirements</a>.';
}

const historySignatures=new Map();
function historyItem(row) {
  const item=document.createElement('div'); item.className='history-row';
  const info=document.createElement('div'), title=document.createElement('strong'), detail=document.createElement('small');
  title.textContent=dateLabel(row.created_at); detail.textContent=row.error || (row.schedule_key ? `Scheduled session ${row.schedule_key}` : 'On-demand research');
  if(row.delivery) detail.textContent+=` · Email: ${row.delivery}`;
  info.append(title,detail);
  const actions=document.createElement('div'); actions.className='history-actions';
  const badge=document.createElement('span'); badge.className=`badge ${row.status}`; badge.textContent=({queued:'Queued',running:'Running',failed:'Failed',partial:'Review coverage',success:'Ready'})[row.status]||row.status; actions.append(badge);
  if(row.summary&&!row.error)detail.textContent=row.summary;
  if(['queued','running'].includes(row.status)) actions.append(button('Cancel', async()=>{await api(`/api/runs/${row.id}/cancel`,{}); await refresh();}));
  else {
    if(row.status!=='failed') actions.append(button('Read report →',()=>openReport(row.id,true)));
    actions.append(button('Delete', async()=>{
      if(!window.confirm('Delete this report? This cannot be undone.')) return;
      await api(`/api/runs/${row.id}`,undefined,'DELETE');
      if(selectedReport===row.id){selectedReport=null;$('#report-card').hidden=true;}
      await refresh(); notice('Report deleted.');
    }));
  }
  item.append(info,actions); return item;
}
function historySignature(row){return JSON.stringify([row.status,row.summary,row.error,row.delivery,row.created_at,row.schedule_key]);}
function renderHistory(rows) {
  // Reconcile by id so a 4-second poll does not rebuild (and steal focus from) unchanged rows.
  const container=$('#history');
  if(!rows.length){
    historySignatures.clear();
    container.replaceChildren(); container.classList.add('empty');
    container.textContent='No research yet. Run a briefing above when ready.';
    return;
  }
  if(container.classList.contains('empty')){container.classList.remove('empty');container.replaceChildren();historySignatures.clear();}
  const existing=new Map([...container.children].filter(el=>el.dataset&&el.dataset.runId).map(el=>[el.dataset.runId,el]));
  const present=new Set();
  for(const row of rows){
    present.add(row.id);
    const signature=historySignature(row);
    if(historySignatures.get(row.id)===signature) continue;
    const item=historyItem(row); item.dataset.runId=row.id;
    const current=existing.get(row.id);
    if(current) current.replaceWith(item); else container.append(item);
    historySignatures.set(row.id,signature);
  }
  for(const [id,el] of existing){if(!present.has(id)){el.remove();historySignatures.delete(id);}}
}
async function refresh(populate=false) {
  state=await api('/api/status');
  const s=state.settings, snapshot=state.snapshot;
  if(!portfolioDirty && editableProjection(snapshot)!==editorVersion) loadPortfolio(snapshot);
  pricesAsOf=state.prices_as_of||pricesAsOf;
  if(snapshot){const serverPrices={};for(const position of snapshot.positions||[]){if(position.price!==null&&position.price!==undefined)serverPrices[position.symbol]={price:position.price,previous_close:position.previous_close};}applyQuotes(serverPrices);}
  updatePricesLabel();
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
    for(const name of ['google_news','nasdaq','bing_news']){const box=$('#research-form').elements.namedItem(`news_source_${name}`);if(box)box.checked=(s.news_feed_sources||[]).includes(name);}
    setForm('#schedule-form',{...s.schedule,scheduled_email:s.scheduled_email});
    if (s.investment_profile) {const targets={...s.investment_profile.allocation_targets};for(const name of ['stocks','bonds','cash']){const input=$('#profile-form').elements.namedItem(`target_${name}`);input.value=targets[name]??'';delete targets[name];}setForm('#profile-form',{...s.investment_profile,allocation_targets:Object.keys(targets).length?JSON.stringify(targets,null,2):'', restrictions:(s.investment_profile.restrictions||[]).join('\n')});}
    if(snapshot) $('#run-source').value='stored';
    const mail=s.smtp;const mailProvider=mail?.host==='smtp.gmail.com'?'gmail':mail?.host==='smtp.mail.me.com'?'icloud':mail?'custom':'gmail';$('#mail-provider').value=mailProvider;setMailProvider(true);
    const now=new Date(); now.setMinutes(now.getMinutes()-now.getTimezoneOffset()); $('#import-form').elements.as_of.value=now.toISOString().slice(0,16); $('#image-form').elements.as_of.value=now.toISOString().slice(0,16);
    const managed=state.externally_managed;
    const nested={model:['#model-form',['provider','model','base_url','temperature','max_tokens']],schedule:['#schedule-form',['enabled','delay_minutes','timezone']],smtp:['#email-form',['host','port','username','sender','recipient','tls_mode','plain_text_only','percent_only']]};
    for(const key of managed) {
      if(nested[key]) {const [form,names]=nested[key];for(const name of names) $(form).elements.namedItem(name).disabled=true;}
      else {const selectorName=key==='model_enabled'?'enabled':key;const form=key==='model_enabled'?$('#model-form'):null;document.querySelectorAll(`[name="${selectorName}"]`).forEach(element=>{if(!form||form.contains(element))element.disabled=true;});}
    }
    if(managed.length) $('#managed-settings').textContent=`Managed by your deployment (read-only here): ${managed.join(', ')}.`;
  }
  renderSecretFields();
  updateSecAvailability();
  renderSecretStorage();
  renderHistory(state.history);
  const activityRun=state.history.find(row=>['queued','running'].includes(row.status))||state.history.find(row=>row.id===selectedReport)||latest;
  if(activityRun){try{renderResearchActivity(await api(`/api/runs/${encodeURIComponent(activityRun.id)}`));}catch{$('#activity-status').textContent='Activity temporarily unavailable; retrying on the next refresh.';}}
  if(latest && ['success','partial'].includes(latest.status) && latest.id!==lastAutoReport && !selectedReport) {openReport(latest.id); lastAutoReport=latest.id;}
  if(pendingRunId){const pending=state.history.find(row=>row.id===pendingRunId);if(pending&&['success','partial','failed'].includes(pending.status)){pendingRunId=null;notice(pending.status==='failed'?'Research failed. Check the run in history.':'Research finished. Conclusions and coverage are shown above.',pending.status==='failed');if(pending.status!=='failed')await openReport(pending.id,true);}}
}
async function refreshQuotes() {
  if(portfolioDirty || document.hidden || !state || !state.snapshot) return;
  let data;
  try { data=await api('/api/quotes'); } catch(error) { return; }
  pricesAsOf=data.as_of||pricesAsOf;
  applyQuotes(data.prices||{});
  renderAllocation(state.snapshot);
  updatePricesLabel();
}
let pollFailed=false;
function setPollStatus(message){const el=alertBox('#poll-status');if(!el)return;setAlertText(el,message||'');el.hidden=!message;}
function secretEditing(){
  const active=document.activeElement;
  if(active&&active.matches&&active.matches('input[type="password"]'))return true;
  return SECRET_FIELDS.some(spec=>{const form=$(spec.form);if(!form||!form.elements)return false;const input=form.elements.namedItem(spec.field);return Boolean(input&&(input.dataset.editing==='true'||document.activeElement===input));});
}
function shouldPausePolling(){return portfolioDirty||secretEditing();}
async function poll(populate=false){
  try{await refresh(populate);pollFailed=false;setPollStatus('');}
  catch(error){if(!pollFailed){pollFailed=true;setPollStatus(`Live updates are paused. ${error.message}`);}}
}
async function save(body, quiet=false) {for(const key of state.externally_managed) delete body[key];if(!Object.keys(body).length)throw new Error('These settings are managed by your deployment environment.');await api('/api/settings',body); await refresh(); if(!quiet) notice('Settings saved.'); }
const PROVIDER_LABELS={colgate:'Colgate LiteLLM',openai:'OpenAI',anthropic:'Anthropic',custom:'Custom'};
const SECRET_FIELDS=[
  {form:'#model-form',field:'key',key:'model_api_key',test:'model',title:'model'},
  {form:'#research-form',field:'key',key:'tavily_api_key',test:'search',title:'Tavily'},
  {form:'#email-form',field:'password',key:'smtp_password',title:'mail password'}
];
function secretText(info) {
  if(!info) return 'No key saved';
  if(info.source==='env'||info.source==='file') {const name=info.managed_by||(info.env&&info.env[0])||'PORTFOLIO_SECRET_...';return `Managed by ${name}`;}
  if(info.source==='alias') {const name=info.managed_by||'an environment variable';return `Using ${name} — save a key here to override`;}
  if(info.present) return `${info.provider?`${PROVIDER_LABELS[info.provider]||info.provider} key`:'Key'} saved${info.saved_at?` ${dateLabel(info.saved_at)}`:''}`;
  return 'No key saved';
}
function renderSecretStatus(box, input, info, spec) {
  const editable=info?info.editable!==false:true, present=Boolean(info&&info.present);
  const managed=Boolean(info&&(info.source==='env'||info.source==='file')), editing=input.dataset.editing==='true';
  const stored=Boolean(info&&info.source==='keyring');
  box.querySelector('.secret-status-text').textContent=secretText(info);
  box.querySelector('.secret-change').hidden=!editable;
  box.querySelector('.secret-remove').hidden=!(editable&&stored);
  box.querySelector('.secret-test').hidden=!(editable&&spec.test);
  box.querySelector('.secret-cancel').hidden=!editing;
  if(managed){input.hidden=true;input.value='';input.placeholder='';input.dataset.editing='false';}
  else if(!present&&editable){input.hidden=false;input.placeholder='Enter secret';}
  else if(!editing){input.value='';input.hidden=true;input.placeholder='';}
}
function renderSecretFields(force=false) {
  for(const spec of SECRET_FIELDS) {
    const input=$(spec.form).elements.namedItem(spec.field), label=input.closest('label');
    let box=label.querySelector('.secret-status');
    if(!box) {
      box=document.createElement('span');box.className='saved-secret secret-status';
      const text=document.createElement('span');text.className='secret-status-text';
      const change=button('Change secret',()=>{input.hidden=false;input.dataset.editing='true';input.placeholder='Enter replacement secret';cancel.hidden=false;input.focus();});change.classList.add('secret-change');
      const remove=button('Remove key',async()=>{if(!window.confirm(`Remove the saved ${spec.title} key?`))return;await api(`/api/secrets/${spec.key}`,undefined,'DELETE');input.value='';input.dataset.editing='false';await refresh();notice('Key removed.');});remove.classList.add('secret-remove');
      const test=button('Test this key',async()=>{const value=input.value.trim();if(!value){notice('Type a key first, or Save to persist it.',true);return;}await api(`/api/test/${spec.test}`,{value});notice('Key verified without saving.');});test.classList.add('secret-test');
      const cancel=button('Cancel',()=>{input.value='';input.dataset.editing='false';renderSecretFields(true);});cancel.classList.add('secret-cancel');
      box.append(text,change,remove,test,cancel);label.append(box);
    }
    // Keep the nested status readable to assistive tech without renaming the field.
    const statusText=box.querySelector('.secret-status-text');
    if(statusText){if(!statusText.id)statusText.id=`secret-status-${spec.key}`;input.setAttribute('aria-describedby',statusText.id);}
    // Do not disturb an in-progress edit; the 4-second poll must leave it alone.
    if((input.dataset.editing==='true'||document.activeElement===input)&&!force)continue;
    renderSecretStatus(box,input,(state&&state.secrets&&state.secrets[spec.key])||null,spec);
  }
}
function renderSecretStorage() {
  const banner=alertBox('#secret-storage');if(!banner)return;
  const storage=state&&state.secret_storage;
  if(!storage||storage.available){banner.hidden=true;return;}
  const names=[...new Set(Object.values((state&&state.secrets)||{}).map(info=>info.env&&info.env[0]).filter(Boolean))];
  banner.hidden=false;
  setAlertText(banner,(storage.reason||'OS credential storage is unavailable.')+(names.length?` Set ${names.slice(0,4).join(' or ')} (a matching _FILE variant also works).`:''));
}
if($('#login-form')) {
  $('#login-form').addEventListener('submit',action(async()=>{await api('/api/login',values('#login-form')); window.location.reload();}));
} else {
  $('#run-form').elements.question.closest('label').after($('#question-readiness'));
  $('#run-form').elements.question.addEventListener('input',updateResearchReadiness);
  $('#run-form').elements.include_suggestions.addEventListener('change',updateResearchReadiness);
  $('.research-card [data-view="connections"]').addEventListener('click',()=>{$('#research-setup').open=true;$('#research-setup').scrollIntoView({behavior:'smooth'});});
  const reportFrame=$('#report-frame'); if(reportFrame) reportFrame.addEventListener('load',sizeReportFrame);
  window.addEventListener('resize',()=>{if(reportFrame&&!$('#report-card').hidden)sizeReportFrame();});
  window.addEventListener('hashchange',()=>{const target=viewFromHash(window.location.hash);if(target&&target!==currentView)view(target,false);});
  const initialView=viewFromHash(window.location.hash); if(initialView) view(initialView,false);
  $('#refresh').addEventListener('click',action(()=>refresh()));
  $('#clear-history').addEventListener('click',action(async()=>{
    const finished=(state.history||[]).filter(row=>!['queued','running'].includes(row.status));
    if(!finished.length){notice('There are no finished reports to clear.');return;}
    if(!window.confirm(`Delete ${finished.length} finished report${finished.length===1?'':'s'}? This cannot be undone.`)) return;
    for(const row of finished){try{await api(`/api/runs/${row.id}`,undefined,'DELETE');}catch(error){notice(error.message,true);break;}}
    if(selectedReport&&finished.some(row=>row.id===selectedReport)&&!state.history.some(row=>row.id===selectedReport&&['queued','running'].includes(row.status))){selectedReport=null;$('#report-card').hidden=true;}
    await refresh(); notice('Finished reports cleared.');
  }));
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
    const positions=rows.map(row=>{const item={};for(const input of row.querySelectorAll('input,select'))item[input.dataset.field]=input.value.trim();item.symbol=item.symbol.toUpperCase();item.quantity=item.quantity?amount(item.quantity,`${item.symbol} shares`):null;const known=pricesBySymbol.get(item.symbol)||{};item.price=priceValue(known.price);item.previous_close=priceValue(known.previous_close);for(const field of ['instrument_name','exchange','sector'])item[field]=item[field]||null;return item;});
    const control=$('#fill-details');control.disabled=true;notice('Looking up missing ticker details and an offline daily-close quote. Your draft will remain unsaved.');
    try{const result=await api('/api/portfolio/enrich',{positions});result.positions.forEach((position,index)=>{const row=rows[index];for(const [field,value] of Object.entries(position)){const input=[...row.querySelectorAll('input,select')].find(element=>element.dataset.field===field);if(input&&value!==null&&value!==undefined)input.value=value;}if(position.price!==null&&position.price!==undefined){pricesBySymbol.set(position.symbol,{price:position.price,previous_close:position.previous_close});const cell=row.querySelector('.holding-price');if(cell)cell.textContent=formatPrice(position.price);}});markPortfolioDirty();const names=result.changes.map(item=>`${item.symbol}: ${item.fields.join(', ')}${item.price_as_of?` (daily close ${item.price_as_of})`:''}${item.source?` via ${item.source}`:''}`);notice(names.length?`Added ${names.join('; ')}. Review and save your portfolio. Quantity is never inferred.`:'No verified missing details were available. Review ticker symbols or enter values manually.');}
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
  $('#model-form').addEventListener('submit',action(async()=>{
    const data=values('#model-form'), key=(data.key||'').trim();
    // Validate/persist settings first; only then store the key, and only then clear the field.
    await save({model:{provider:data.provider,model:data.model,base_url:data.base_url,temperature:Number(data.temperature),max_tokens:Number(data.max_tokens)},model_enabled:checked('#model-form','enabled')},true);
    if(key){await api('/api/secrets/model_api_key',{value:key});const input=$('#model-form').elements.key;input.value='';input.dataset.editing='false';await refresh();}
    notice('Model settings saved.');
  }));
  $('#research-form').addEventListener('submit',action(async()=>{
    const data=values('#research-form'), tavily=(data.key||'').trim();
    await save({search_provider:data.search_provider,sec_contact:data.sec_contact,primary_feeds_enabled:checked('#research-form','primary_feeds_enabled'),sec_company_filings_enabled:checked('#research-form','sec_company_filings_enabled'),ticker_news_enabled:checked('#research-form','ticker_news_enabled'),news_feed_sources:['google_news','nasdaq','bing_news'].filter(name=>checked('#research-form',`news_source_${name}`)),auto_prices_enabled:checked('#research-form','auto_prices_enabled'),price_refresh_minutes:Number(data.price_refresh_minutes),market_data_enabled:checked('#research-form','market_data_enabled')},true);
    // Only store a provider's key when that provider is actually selected.
    if(data.search_provider==='tavily'&&tavily){await api('/api/secrets/tavily_api_key',{value:tavily});const input=$('#research-form').elements.key;input.value='';input.dataset.editing='false';}
    await refresh(); notice('Research settings saved.');
  }));
  document.querySelectorAll('[data-test]').forEach(el=>el.addEventListener('click',action(async()=>{el.disabled=true;notice('Testing the saved connection…');try{const result=await api(`/api/test/${el.dataset.test}`,{});notice(result.message);}finally{el.disabled=false;await refresh();}})));
  $('#research-form').elements.search_provider.addEventListener('change',()=>{$('#tavily-fields').hidden=$('#research-form').elements.search_provider.value!=='tavily';});
  $('#research-form').elements.sec_company_filings_enabled.addEventListener('change',event=>{event.target.dataset.userSelected='true';updateSecAvailability();});
  $('#research-form').elements.sec_contact.addEventListener('input',event=>{
    const box=$('#research-form').elements.sec_company_filings_enabled;
    if(!state.settings.sec_contact&&!box.disabled&&!box.dataset.userSelected&&/[^\s@]+@[^\s@]+\.[^\s@]+/.test(event.target.value))box.checked=true;
    updateSecAvailability();
  });
  $('#research-form').elements.ticker_news_enabled.addEventListener('change',()=>{$('#news-source-fields').hidden=!checked('#research-form','ticker_news_enabled');});
  $('#preferences-form').addEventListener('submit',action(async()=>{const data=values('#preferences-form');for(const key of ['position_review_threshold_pct','sector_review_threshold_pct','max_searches','max_model_calls','max_tool_calls','max_seconds','max_data_age_hours','retention_days']) data[key]=Number(data[key]);await save(data);}));
  $('#profile-form').addEventListener('submit',action(async()=>{const data=values('#profile-form');let targets={};try{targets=data.allocation_targets.trim()?JSON.parse(data.allocation_targets):{};}catch{throw new Error('Other targets must be a JSON object, for example {"real_estate":10}.');}if(!targets||Array.isArray(targets)||typeof targets!=='object')throw new Error('Other targets must be a JSON object.');for(const name of ['stocks','bonds','cash']){const raw=data[`target_${name}`].trim();delete data[`target_${name}`];if(raw)targets[name]=Number(raw);}if(!Object.keys(targets).length||Object.values(targets).some(value=>!Number.isFinite(value)||value<0||value>100)||Object.values(targets).reduce((sum,value)=>sum+value,0)>100)throw new Error('Enter targets between 0% and 100% that total no more than 100%.');data.allocation_targets=targets;data.restrictions=data.restrictions.split('\n').map(s=>s.trim()).filter(Boolean);await save({investment_profile:data});}));
  $('#mail-provider').addEventListener('change',()=>setMailProvider());
  $('#email-form').addEventListener('submit',action(async()=>{
    const data=values('#email-form');const provider=data.mail_provider;delete data.mail_provider;
    if(provider!=='custom'&&!data.username)data.username=data.sender;
    const password=(data.password||'').trim();delete data.password;
    data.port=Number(data.port);data.plain_text_only=checked('#email-form','plain_text_only');data.percent_only=checked('#email-form','percent_only');
    await save({smtp:data,scheduled_email:false},true);
    if(password){await api('/api/secrets/smtp_password',{value:password});const input=$('#email-form').elements.password;input.value='';input.dataset.editing='false';}
    await refresh(); notice('Mail settings saved.');
  }));
  $('#request-code').addEventListener('click',action(async()=>{const result=await api('/api/email/verification',{});notice(result.message,result.status!=='sent');}));
  $('#verify-form').addEventListener('submit',action(async()=>{await api('/api/email/verify',values('#verify-form'));await refresh();notice('Your inbox is verified. Delivery is now available when you choose it.');}));
  $('#schedule-form').addEventListener('submit',action(async()=>{const data=values('#schedule-form');await save({schedule:{enabled:checked('#schedule-form','enabled'),delay_minutes:Number(data.delay_minutes),timezone:data.timezone},scheduled_email:checked('#schedule-form','scheduled_email')});}));
  $('#send-report').addEventListener('click',action(async()=>{const result=await api(`/api/runs/${selectedReport}/send`,{});notice(result.message,result.status!=='sent');await refresh();}));
  poll(true);
  setInterval(()=>{if(!shouldPausePolling())poll();},4000);
  setInterval(()=>refreshQuotes().catch(()=>{}),45000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshQuotes().catch(()=>{});});
}
