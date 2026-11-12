'use strict';
const $=id=>document.getElementById(id);
const state={macro:null,pairBatch:null,archive:[],symbols:[],instruments:[],configured:false,busy:false,selection:0,currentMonth:'',provider:'gemini',connectionDetail:''};
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels={very_bearish:'Very bearish',bearish:'Bearish',neutral:'Neutral / mixed',bullish:'Bullish',very_bullish:'Very bullish',insufficient:'Insufficient evidence'};
const topics={price_tone:'Price tone',macro_tone:'Macro / policy',risk_tone:'Risk / volatility',regional_tone:'Asia / global',news_tone:'Headline tone'};
const monthTitle=value=>new Date(value+'-01T12:00:00Z').toLocaleDateString('en-GB',{month:'long',year:'numeric',timeZone:'UTC'});
const money=x=>`${x<0?'−':'+'}$${Math.abs(x).toLocaleString('en-US')}`;
const kind=d=>d.kind||'macro';const view=d=>d.perspective||'outlook';
const scope=d=>d.report_format==='sentiment-v1'&&d.target_month===$('target').value&&view(d)===$('perspective').value;
const cost=d=>d.estimated_cost_usd==null?'':` · estimated $${d.estimated_cost_usd.toFixed(4)}`;
const number=x=>x==null?'—':Number(x).toLocaleString('en-US',{maximumFractionDigits:10});
const figure=x=>x==null?'—':Number(x).toLocaleString('en-US',{maximumSignificantDigits:8});
const instrument=s=>state.instruments.find(i=>i.symbol===s)||{symbol:s,label:s,venue:'Archived instrument'};
const instrumentLabel=s=>instrument(s).label;
const archiveTitle=d=>kind(d)==='pairs'?(d.symbols||[]).map(instrumentLabel).join(', ')||'Pairs':'Macro';
function instrumentOptions(symbols,first){return `<option value="">${esc(first)}</option>`+symbols.map(s=>`<option value="${esc(s)}">${esc(instrumentLabel(s))}</option>`).join('');}
async function api(path,body){const r=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw new Error(data.error||'Request failed');return data;}
function error(message=''){$('research-error').hidden=!message;$('research-error').textContent=message;}
function providerNote(){const p=$('provider').value,gemini=p==='gemini';$('api-key-field').hidden=p==='codex';$('api-key-label').textContent=gemini?'Gemini API key (free)':'Anthropic API key';$('api-key').placeholder=gemini?(state.provider==='gemini'&&state.configured?'Saved · paste to replace':'AIza…'):'sk-ant-…';$('provider-note').textContent=gemini?'Gemini 3.8 Flash on Google’s free tier: create a free key at aistudio.google.com/apikey (no card). Free-tier limits are small (roughly 20 requests/day; each macro or instrument refresh is one request). On the free tier Google may use prompts to improve its products; one-month market observations and headline metadata are sent to Google, trade imports stay local.':p==='codex'?'GPT-6.1 Sol through Codex CLI, signed in with ChatGPT. Uses your plan’s Codex allowance and limits. No API key needed. If disconnected, run codex login in your terminal. One-month market observations and headline metadata are sent to OpenAI; trade imports stay local.':'Claude Haiku 4.5 uses separately billed Anthropic API credit. The key is stored locally; one-month market observations and headline metadata are sent to Anthropic.';}
function buttonLabels(){$('generate').textContent=state.macro?'Refresh macro AI ↻':'Load macro AI ↗';const selected=$('pair-filter').value,hasReport=(state.pairBatch?.analysis.pairs||[]).some(pair=>pair.symbol===selected);$('generate-pairs').textContent=hasReport?'Refresh selected ↻':'Analyze selected ↗';$('generate-pairs').disabled=state.busy||!selected;$('generate-both').disabled=state.busy||!selected;}
function busy(value){state.busy=value;for(const id of ['generate','target','perspective','save-key','pair-filter'])$(id).disabled=value;buttonLabels();document.querySelectorAll('[data-archive]').forEach(b=>b.disabled=value);}
function safeUrl(value){try{const u=new URL(value);return u.protocol==='https:'?u.href:null;}catch{return null;}}
function prose(paragraphs,sources){const known=new Map((sources||[]).map(row=>[row.id,row]));return (paragraphs||[]).map(text=>'<p>'+esc(text).replace(/\[(S\d+)\]/g,(all,id)=>{const source=known.get(id),url=source&&safeUrl(source.url);return url?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" title="${esc(source.title)}">${all}</a>`:all;})+'</p>').join('');}
function sourceLinks(d){const rows=d.source_ledger||[];return `<details class="source-ledger"><summary>Sources · ${rows.length} links · fetched data, searches and used headline metadata</summary><p class="small muted">The model receives these inputs; it does not browse independently. Headline links do not mean article bodies were read. Cached snapshots retain their original retrieval time.</p><ol>${rows.map(row=>{const url=safeUrl(row.url);return `<li><a href="${esc(url||'#')}" target="_blank" rel="noopener noreferrer">[${esc(row.id)}] ${esc(row.title)}</a><small>${esc(row.kind.replaceAll('_',' '))} · ${row.access==='article_not_fetched'?'Headline metadata only; article not fetched':row.access==='cached_snapshot'?'Cached input snapshot':'Fetched by data collector'}${row.available?'':' · unavailable / not used as evidence'}${row.retrieved_at?' · '+esc(row.retrieved_at.slice(0,16).replace('T',' '))+' UTC':''}${row.published_at?' · published '+esc(row.published_at.slice(0,10)):''}</small></li>`;}).join('')}</ol></details>`;}
function scorePanel(a,sources){const n=a.score;return `<div class="score-heading"><strong class="tone-${a.sentiment}">${n==null?'—':(n>0?'+':'')+n}</strong><div><b>${esc(labels[a.sentiment])}</b><small>${a.rated_topics}/5 evidenced topics · −100 bearish to +100 bullish</small></div></div>${n==null?'':'<meter min="-100" max="100" low="-20" high="20" optimum="100" value="'+n+'" aria-label="AI-rated sentiment tone: '+n+' out of a range from minus 100 to plus 100"></meter>'}<p class="small muted">${esc(a.rubric)}</p><div class="score-components">${a.components.map(c=>`<div><span>${esc(topics[c.key])}</span><b>${c.rating==null?'Unavailable':(c.rating>0?'+':'')+c.rating}</b><div>${prose([c.reason+' '+c.source_ids.map(id=>'['+id+']').join(' ')],sources)}</div></div>`).join('')}</div>`;}
const signed=n=>n==null?'—':(n>0?'+':'')+n;
const tierNames={major:'Market-moving',market:'Broad crypto',coin:'Coin-specific'};
const driftNames={improving:'↗ improving',deteriorating:'↘ deteriorating',stable:'→ stable'};
function pulsePanel(pair,sources){const p=pair.news_pulse,c=pair.combined;if(!p||!c)return '';
  const tile=(title,score,tone,note)=>`<div><small>${title}</small><strong class="tone-${tone}">${signed(score)}</strong><span class="tone-${tone}">${esc(labels[tone])}</span>${note?`<small>${esc(note)}</small>`:''}</div>`;
  return `<div class="pulse-now">${tile('Current tone',c.score_now,c.label_now,'monthly index '+signed(pair.score)+' ⊕ news pulse '+signed(p.score_now))}${tile('Next few days',c.score_next,c.label_next,c.drift?driftNames[c.drift]:'')}${tile('News pulse now → next',p.score_now,p.label_now,'projected '+signed(p.score_next)+' · '+p.window_start+' → '+p.window_end+(p.live?' (live)':''))}</div>`+
  `<div class="narratives">${p.narratives.length?p.narratives.map(n=>`<div><div><b class="tier-${n.tier}">${tierNames[n.tier]}</b><small>${esc(n.momentum)} · ${n.newest_age_days.toFixed(1)}d old</small></div><div><b>${esc(n.title)}</b>${prose([n.reason+' '+n.source_ids.map(id=>'['+id+']').join(' ')],sources)}</div><div><b>${signed(n.rating)} → ${signed(n.outlook)}</b><small>weight ${n.weight_now} now · ${n.weight_next} next</small><small>freshness ${n.freshness}</small></div></div>`).join(''):'<p class="small muted">No recent narratives were identified in the supplied headlines.</p>'}</div>`+
  `<p class="small muted">${esc(p.rubric)} ${esc(c.rubric)}</p>`;}
function previewWindow(){const value=$('target').value;if(!value)return;const [year,month]=value.split('-').map(Number),prior=$('perspective').value==='outlook';const start=new Date(Date.UTC(year,month-1-(prior?1:0),1)),monthEnd=new Date(Date.UTC(year,month-(prior?1:0),0)),now=new Date(),yesterday=new Date(Date.UTC(now.getUTCFullYear(),now.getUTCMonth(),now.getUTCDate()-1));const end=new Date(Math.min(monthEnd,yesterday));$('evidence-window').textContent=end<start?'No completed days in this month yet.':`One-month evidence window: ${start.toISOString().slice(0,10)} → ${end.toISOString().slice(0,10)}${prior?' · previous month only':' · selected month only'}`;}
function showMacro(d){
  state.macro=d;const a=d.analysis;$('evidence-window').textContent=`Saved evidence window: ${d.observation_start} → ${d.data_cutoff} · one observation month`;$('brief').hidden=false;$('empty').hidden=true;
  $('sentiment-label').textContent=labels[a.sentiment];$('sentiment-label').className='verdict tone-'+a.sentiment;
  $('window-note').textContent=`Evidence window: ${d.observation_start} → ${d.data_cutoff}`;
  $('summary').innerHTML=prose(a.paragraphs,d.source_ledger);$('sentiment-score').innerHTML=scorePanel(a,d.source_ledger);
  $('limitation').textContent=a.limitation;
  $('provenance').textContent=`${d.reconstruction?'Historical reconstruction · ':''}${view(d)==='monitor'?'Selected-month sentiment':'Previous-month sentiment context'} · ${new Date(d.generated_at).toLocaleString()} · ${d.model}${cost(d)}`;
  $('sources-section').innerHTML=sourceLinks(d);$('analysis-status').textContent='Saved sentiment report. Refresh to generate a new AI analysis.';buttonLabels();renderPairs();
}
function renderPairs(){
  const d=state.pairBatch,selected=$('pair-filter').value,selectedPair=(d?.analysis.pairs||[]).find(pair=>pair.symbol===selected);buttonLabels();
  $('pair-brief').hidden=!selectedPair;$('pair-status').className='small muted';
  if(!selected){$('pair-status').textContent=`${state.instruments.length} USDT favorites · choose one for its report.`;return;}
  if(!selectedPair){$('pair-status').textContent=`No saved ${$('perspective').value==='outlook'?'before-the-month':'during-the-month'} report for ${instrumentLabel(selected)}. Analyze it to generate one.`;return;}
  $('pair-brief-title').textContent=`${instrumentLabel(selected)} · ${instrument(selected).venue}`;const combo=selectedPair.combined;$('pair-brief-verdict').textContent=combo?`Now ${labels[combo.label_now]} ${signed(combo.score_now)} · next days ${signed(combo.score_next)}${combo.drift?' '+driftNames[combo.drift]:''}`:`${labels[selectedPair.sentiment]} · ${signed(selectedPair.score)}`;$('pair-brief-verdict').className='tone-'+(combo?combo.label_now:selectedPair.sentiment)+' small';$('pair-pulse').innerHTML=pulsePanel(selectedPair,d.source_ledger);$('pair-pulse-section').hidden=!combo;
  $('pair-summary').innerHTML=prose(selectedPair.paragraphs,d.source_ledger);$('pair-score').innerHTML=scorePanel(selectedPair,d.source_ledger);$('pair-limitation').textContent=selectedPair.limitation;$('pair-sources').innerHTML=sourceLinks(d);
  const stale=(state.macro&&d.macro_analysis_id!==state.macro.id);
  $('pair-status').textContent=stale?'Macro report changed since this crypto report. Refresh it to link the new macro view.':`Linked macro sentiment version ${d.macro_analysis_id.slice(0,8)} · scores describe tone, not trading suitability.`;
  $('pair-status').className='small '+(stale?'negative':'muted');
  $('pair-provenance').textContent=`Evidence ${d.observation_start} → ${d.data_cutoff} · ${new Date(d.generated_at).toLocaleString()} · ${d.model}${cost(d)}`;
}
async function selectPair(){
  const symbol=$('pair-filter').value;
  if(!symbol||(state.pairBatch?.analysis.pairs||[]).some(p=>p.symbol===symbol))return renderPairs();
  const saved=state.archive.filter(d=>scope(d)&&kind(d)==='pairs'&&(d.symbols||[]).includes(symbol));
  const match=saved.find(d=>d.macro_analysis_id===state.macro?.id)||saved[0];
  if(!match){state.pairBatch=null;return renderPairs();}
  const selection=++state.selection;
  try{const d=await api('/api/archive/'+match.id);if(selection!==state.selection)return;state.pairBatch=d;renderPairs();}catch(e){if(selection===state.selection)error(e.message);}
}
async function selectMonth(){
  if(state.busy||!$('target').value)return;
  if($('target').value>state.currentMonth)$('perspective').value='outlook';
  previewWindow();const selection=++state.selection;error();$('research-title').textContent=monthTitle($('target').value);
  state.macro=null;state.pairBatch=null;$('brief').hidden=true;$('empty').hidden=false;renderPairs();
  const macro=state.archive.find(d=>scope(d)&&kind(d)==='macro'),pairs=state.archive.find(d=>scope(d)&&kind(d)==='pairs');
  $('analysis-status').textContent=macro?'Opening saved analyses…':state.configured?'Ready to load macro AI.':state.connectionDetail;
  try{const docs=await Promise.all([macro?api('/api/archive/'+macro.id):null,pairs?api('/api/archive/'+pairs.id):null]);if(selection!==state.selection)return;if(docs[1]){state.pairBatch=docs[1];const savedSymbol=docs[1].analysis.pairs?.[0]?.symbol;if(savedSymbol&&state.symbols.includes(savedSymbol))$('pair-filter').value=savedSymbol;}if(docs[0])showMacro(docs[0]);else renderPairs();}
  catch(e){if(selection===state.selection)error(e.message);}
}
async function refreshArchive(){
  state.archive=(await api('/api/archive')).analyses;const reports=state.archive.filter(d=>d.report_format==='sentiment-v1');const months=new Set(reports.map(d=>d.target_month)).size;
  $('archive-count').textContent=`${reports.length} saved · showing ${Math.min(10,reports.length)} latest`;
  const priced=state.archive.filter(d=>d.estimated_cost_usd!=null),total=priced.reduce((sum,d)=>sum+d.estimated_cost_usd,0);
  $('cost-note').textContent=state.provider==='gemini'?`Google Gemini 3.8 Flash · free tier, no charge from this helper. One macro request plus one per refreshed instrument count toward the daily free limit. Opening saved summaries makes no new model call.`:state.provider==='codex'?`OpenAI GPT-6.1 Sol · uses your ChatGPT plan’s Codex allowance. No API key or per-token API charge from this helper. Opening saved summaries makes no new model call; each refresh uses your allowance.`:`One macro call plus one call per selected instrument. Paid API token usage varies with report length; saved-call cost estimate so far: $${total.toFixed(4)}${priced.length<state.archive.length?' (some older versions unpriced)':''}. Opening saved versions is free.`;
  $('archive-list').innerHTML=reports.length?reports.slice(0,10).map(d=>`<button class="archive-item" data-archive="${d.id}"><span><b>${esc(monthTitle(d.target_month))} · ${esc(archiveTitle(d))}</b><small>${view(d)==='monitor'?'During month':'Before month'}${d.reconstruction?' · reconstruction':''} · ${esc(new Date(d.generated_at).toLocaleString())}</small></span><span class="archive-verdict tone-${d.sentiment}">${esc(labels[d.sentiment])} · ${d.score==null?'—':d.score}</span><span class="archive-cutoff">Data to ${d.data_cutoff}<span>Open →</span></span></button>`).join(''):'<p class="muted">Your sentiment report versions will appear here. Earlier strategy-format reports remain preserved in the database.</p>';
  document.querySelectorAll('[data-archive]').forEach(button=>button.addEventListener('click',()=>openArchive(button.dataset.archive)));
}
async function openArchive(id){
  if(state.busy)return;const selection=++state.selection;error();
  try{const d=await api('/api/archive/'+id);if(selection!==state.selection)return;$('target').value=d.target_month;$('perspective').value=view(d);$('research-title').textContent=monthTitle(d.target_month);state.macro=null;state.pairBatch=null;
    if(kind(d)==='pairs'){const macro=await api('/api/archive/'+d.macro_analysis_id);if(selection!==state.selection)return;state.pairBatch=d;const savedSymbol=d.analysis.pairs?.[0]?.symbol;if(savedSymbol&&state.symbols.includes(savedSymbol))$('pair-filter').value=savedSymbol;showMacro(macro);}
    else{const pairs=state.archive.find(p=>kind(p)==='pairs'&&p.macro_analysis_id===d.id);if(pairs){const p=await api('/api/archive/'+pairs.id);if(selection!==state.selection)return;state.pairBatch=p;}showMacro(d);}
    $('research').scrollIntoView({behavior:'smooth'});
  }catch(e){if(selection===state.selection)error(e.message);}
}
async function generate(which){
  if(state.busy)return;error();const both=which==='both',selectedSymbol=$('pair-filter').value;
  if(!state.configured){$('ai-settings').open=true;$(state.provider==='codex'?'provider':'api-key').focus();error(state.connectionDetail);return;}
  if((which==='pairs'||both)&&!selectedSymbol){error('Choose a crypto first.');return;}
  if(which==='pairs'&&!state.macro){error('Load macro AI first, or use Refresh macro + selected to run both in order.');return;}
  ++state.selection;busy(true);
  try{
    let macro=state.macro,macroResult=null,pairResult=null;
    if(which==='macro'||both){$('analysis-status').textContent='Refreshing macro evidence and generating its sentiment report…';macroResult=await api('/api/analysis',{target_month:$('target').value,perspective:$('perspective').value,regenerate:true});macro=macroResult;showMacro(macro);}
    if(which==='pairs'||both){$('analysis-status').textContent=`${both?'Macro refreshed. Now ':'Refreshing '}${instrumentLabel(selectedSymbol)} evidence and industry headlines…`;pairResult=await api('/api/analysis/pairs',{target_month:$('target').value,perspective:$('perspective').value,regenerate:true,macro_id:macro.id,symbol:selectedSymbol});state.pairBatch=pairResult;renderPairs();$('analysis-status').textContent=`${instrumentLabel(selectedSymbol)} sentiment saved for ${$('perspective').value==='outlook'?'before the month':'during the month'} with related industry headline evidence.`;}
    const result=pairResult||macroResult;if(result?.reused_unchanged)$('analysis-status').textContent='No new evidence found. Reused the saved summary without another AI charge.';
    await refreshArchive();
  }catch(e){error(e.message);$('analysis-status').textContent='Previous saved analyses are preserved.';}finally{busy(false);}
}
async function saveKey(){if(state.busy)return;error();$('save-key').disabled=true;try{const d=await api('/api/ai/settings',{provider:$('provider').value,api_key:$('api-key').value});state.configured=d.configured;state.connectionDetail=d.detail;state.provider=d.provider;$('api-key').value='';providerNote();$('ai-settings').open=false;$('key-status').textContent='· '+d.detail;$('analysis-status').textContent=d.detail;await refreshArchive();}catch(e){$('settings-status').textContent=e.message;}finally{$('save-key').disabled=false;}}
function download(text,type,name){const url=URL.createObjectURL(new Blob([text],{type}));const link=document.createElement('a');link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function exportSummary(){const d=state.macro;if(!d)return;const report=doc=>{const a=doc.analysis;return `# ${monthTitle(doc.target_month)} — ${labels[a.sentiment]}

Evidence: ${doc.observation_start} to ${doc.data_cutoff} (${view(doc)})

${a.paragraphs.join('\n\n')}

## Sentiment score: ${a.score??'Unavailable'}
${a.rubric}

${a.components.map(c=>`- ${topics[c.key]}: ${c.rating??'Unavailable'} — ${c.reason} ${c.source_ids.map(id=>'['+id+']').join(' ')}`).join('\n')}

${a.limitation}

Model: ${doc.model}; generated: ${doc.generated_at}${cost(doc)}
`;};let text=report(d);if(state.pairBatch?.macro_analysis_id===d.id){const p=state.pairBatch;text+='\n## Instrument batch\n\n'+report(p)+'\n'+p.analysis.pairs.map(r=>`### ${instrumentLabel(r.symbol)} — ${labels[r.sentiment]} (${r.score??'Unavailable'})\n\n${r.paragraphs.join('\n\n')}\n\n${r.combined?`**Current tone ${signed(r.combined.score_now)} (${labels[r.combined.label_now]}) · next few days ${signed(r.combined.score_next)} (${labels[r.combined.label_next]})${r.combined.drift?' · '+r.combined.drift:''}** — news pulse ${signed(r.news_pulse.score_now)} → ${signed(r.news_pulse.score_next)}\n\n${r.news_pulse.narratives.map(n=>`- ${tierNames[n.tier]} · ${n.momentum}: ${n.title} — ${signed(n.rating)} → ${signed(n.outlook)} (weight ${n.weight_now}/${n.weight_next}) ${n.reason} ${n.source_ids.map(id=>'['+id+']').join(' ')}`).join('\n')}\n\n`:''}${r.limitation}`).join('\n\n');}const sources=state.pairBatch?.macro_analysis_id===d.id?state.pairBatch.source_ledger:d.source_ledger;text+='\n\n## All source links\n\n'+sources.map(row=>`- [${row.id}] [${row.title}](${row.url}) — ${row.kind}; ${row.access}${row.available?'':' / unavailable'}; ${row.retrieved_at||row.published_at||''}`).join('\n');download(text,'text/markdown;charset=utf-8',`sentiment-${d.target_month}-${view(d)}-${d.id.slice(0,8)}.md`);}

function tradeQuery(){return new URLSearchParams(Object.fromEntries([['side',$('trade-side').value],['symbol',$('trade-symbol').value],['month',$('trade-month').value]].filter(([,v])=>v)));}
let tradeRequest=0,tradeOffset=0;const TRADE_PAGE=50;
async function loadTrades(offset=0){const request=++tradeRequest;try{const query=tradeQuery();query.set('limit',TRADE_PAGE);query.set('offset',offset);const d=await api('/api/trades?'+query);if(request!==tradeRequest)return;const selected=$('trade-symbol').value;$('trade-symbol').innerHTML=instrumentOptions([...new Set([...state.symbols,...d.symbols])].sort(),'All instruments');$('trade-symbol').value=selected;
    if(d.total&&offset>=d.total)return loadTrades(Math.floor((d.total-1)/TRADE_PAGE)*TRADE_PAGE);tradeOffset=offset;const pages=Math.max(1,Math.ceil(d.total/TRADE_PAGE));
    $('trade-count').textContent=`${d.total} trades`;$('trade-page').textContent=`Page ${Math.floor(offset/TRADE_PAGE)+1} of ${pages}`;$('trade-prev').disabled=offset===0;$('trade-next').disabled=offset+TRADE_PAGE>=d.total;
    $('trade-status').textContent=`${d.total} matching trades · ${(d.counts.executed||0)+(d.counts.paper||0)} events in archive${d.total>TRADE_PAGE?` · showing ${offset+1}–${offset+d.trades.length}`:''}.`;
    $('trade-rows').innerHTML=d.trades.length?d.trades.map(t=>`<tr><td>${t.closed_at?esc(t.closed_at.slice(0,19).replace('T',' ')):'open'}</td><td>${esc(instrumentLabel(t.symbol))}</td><td>${esc(t.side||'—')}</td><td>${figure(t.entry_price)}</td><td>${figure(t.exit_price)}</td><td>${figure(t.reported_pnl)}</td></tr>`).join(''):'<tr><td colspan="6" class="muted">No trade events archived for these filters.</td></tr>';
  }catch(e){if(request===tradeRequest)$('trade-status').textContent=e.message;}}
 $('generate').addEventListener('click',()=>generate('macro'));$('generate-pairs').addEventListener('click',()=>generate('pairs'));$('generate-both').addEventListener('click',()=>generate('both'));$('save-key').addEventListener('click',saveKey);$('target').addEventListener('change',selectMonth);$('perspective').addEventListener('change',()=>{if($('perspective').value==='monitor'&&$('target').value>state.currentMonth)$('target').value=state.currentMonth;selectMonth();});$('pair-filter').addEventListener('change',selectPair);$('export').addEventListener('click',exportSummary);
$('provider').addEventListener('change',providerNote);
for(const id of ['trade-side','trade-symbol','trade-month'])$(id).addEventListener('change',()=>loadTrades());
$('trade-prev').addEventListener('click',()=>loadTrades(Math.max(0,tradeOffset-TRADE_PAGE)));$('trade-next').addEventListener('click',()=>loadTrades(tradeOffset+TRADE_PAGE));
// Fills arrive from the desk's background sync; keep the table current without a page reload.
setInterval(()=>{if(!document.hidden&&document.activeElement!==$('trade-symbol'))loadTrades(tradeOffset);},15000);
document.querySelector('.workspace-nav a[href="#trade-archive"]').addEventListener('click',()=>{$('trade-collapse').open=true;});
$('clear-trade-filters').addEventListener('click',()=>{$('trade-month').value='';loadTrades();});$('export-trades').addEventListener('click',async()=>{try{const r=await fetch('/api/trades/export?'+tradeQuery());if(!r.ok)throw new Error((await r.json()).error);download(await r.text(),'text/csv;charset=utf-8','regime-trade-events.csv');}catch(e){$('trade-status').textContent=e.message;}});
(async()=>{try{const [status,pairs]=await Promise.all([api('/api/ai/status'),api('/api/pairs')]);state.configured=status.configured;state.connectionDetail=status.detail;state.provider=status.provider;$('provider').value=status.provider;providerNote();state.symbols=pairs.symbols;state.instruments=pairs.instruments;state.currentMonth=status.current_month;$('target').value=status.current_month;$('trade-month').value=status.current_month;$('target').max=status.default_target;$('key-status').textContent='· '+status.detail;$('ai-settings').open=!status.configured;$('pair-filter').innerHTML=instrumentOptions(state.symbols,'Select a crypto');await refreshArchive();await selectMonth();await loadTrades();}catch(e){error(e.message);$('analysis-status').textContent='Unable to load the local research desk.';}})();
