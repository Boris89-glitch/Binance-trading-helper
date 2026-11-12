"""Grounded monthly AI briefs and immutable local SQLite archives."""
import calendar
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid

LOCAL_DIR = Path(__file__).resolve().parents[2] / '.macro_helper_local'
GENERATION_LOCK = threading.Lock()
CONFIG_LOCK = threading.Lock()
DEFAULT_MODEL = 'claude-haiku-4-5'
PROVIDERS = ('gemini','codex','anthropic')
from .sentiment import SYSTEM,PAIR_SYSTEM,FORMAT,validate_output,validate_pair_output


def now_utc():
    return datetime.now(timezone.utc)


def settings():
    try:
        saved = json.loads((LOCAL_DIR/'settings.json').read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        saved = {}
    provider=saved.get('provider','gemini')
    if provider not in PROVIDERS:provider='gemini'
    model=default_model(provider)
    if provider=='gemini':key=os.environ.get('GEMINI_API_KEY') or saved.get('gemini_api_key','')
    elif provider=='anthropic':key=os.environ.get('ANTHROPIC_API_KEY') or saved.get('api_key','')
    else:key=''
    return {'provider':provider,'api_key':key,
            'model':saved.get('model',model) if saved.get('provider')==provider else model}


def default_model(provider):
    from .gemini_model import DEFAULT_MODEL as GEMINI_MODEL
    return {'codex':'gpt-6.1-sol','gemini':GEMINI_MODEL}.get(provider,DEFAULT_MODEL)


def require_key(config):
    if config.get('provider')!='codex' and not config['api_key']:
        raise ValueError('Add your {} API key in AI settings first'.format('Gemini' if config.get('provider')=='gemini' else 'Anthropic'))


def settings_status():
    config=settings()
    if config['provider']=='codex':
        from .codex_model import status
        connection=status()
    else:
        name='Gemini' if config['provider']=='gemini' else 'Anthropic'
        connection={'configured':bool(config['api_key']),'detail':f'{name} API key configured' if config['api_key'] else f'{name} API key needed'}
    return {**connection,'provider':config['provider'],'model':config['model'],
            'billing_mode':{'codex':'chatgpt_subscription','gemini':'free_tier'}.get(config['provider'],'api'),
            'default_target':next_month(now_utc().date()).strftime('%Y-%m'),
            'current_month':now_utc().strftime('%Y-%m')}


def save_settings(data):
    provider=data.get('provider','gemini')
    if provider not in PROVIDERS:raise ValueError('Choose Gemini, OpenAI subscription or Anthropic API')
    key=data.get('api_key','')
    if provider=='gemini':
        from .gemini_model import valid_key
        if not isinstance(key,str):key=''
        if not key.strip() and not settings_key('gemini_api_key') and not os.environ.get('GEMINI_API_KEY'):
            raise ValueError('Enter your free Gemini API key from aistudio.google.com')
        if key.strip() and not valid_key(key):raise ValueError('This does not look like a Gemini API key')
    if provider=='anthropic' and (not isinstance(key,str) or not key.strip()):
        raise ValueError('Enter your Anthropic API key')
    if provider=='anthropic' and (not key.strip().startswith('sk-ant-') or len(key.strip())>500):
        raise ValueError('This does not look like an Anthropic API key')
    with CONFIG_LOCK:
        LOCAL_DIR.mkdir(exist_ok=True)
        path=LOCAL_DIR/'settings.json'
        temp=path.with_suffix('.tmp')
        try:saved=json.loads(path.read_text(encoding='utf-8'))
        except (FileNotFoundError,ValueError):saved={}
        saved.update(provider=provider,model=default_model(provider))
        if provider=='anthropic':saved['api_key']=key.strip()
        if provider=='gemini' and key.strip():saved['gemini_api_key']=key.strip()
        temp.write_text(json.dumps(saved),encoding='utf-8')
        temp.replace(path)
    return settings_status()


def settings_key(name):
    try:return json.loads((LOCAL_DIR/'settings.json').read_text(encoding='utf-8')).get(name,'')
    except (FileNotFoundError,ValueError):return ''


def next_month(day):
    return (day.replace(day=1)+timedelta(days=32)).replace(day=1)


def forecast_window(target, today=None, perspective='outlook'):
    try:
        selected=datetime.strptime(target,'%Y-%m').date().replace(day=1)
    except (ValueError,TypeError):
        raise ValueError('Choose a valid target month') from None
    if selected.strftime('%Y-%m')!=target:
        raise ValueError('Use YYYY-MM for the target month')
    today=today or now_utc().date()
    if selected < date(2025,1,1) or selected > next_month(today):
        raise ValueError('Choose a target month from January 2025 through next month')
    if perspective not in ('outlook','monitor'):
        raise ValueError('Choose outlook or monitor perspective')
    end=min((next_month(selected) if perspective=='monitor' else selected)-timedelta(days=1),today-timedelta(days=1))
    if perspective=='monitor' and end<selected:
        raise ValueError('This month has no completed days yet; use the outlook perspective')
    start=selected if perspective=='monitor' else (selected-timedelta(days=1)).replace(day=1)
    return selected,start,end


def past_results(results, cutoff):
    rows=[]
    for year,record in results.items():
        for i,pnl in enumerate(record['pnl']):
            final=date(int(year),i+1,calendar.monthrange(int(year),i+1)[1])
            if (int(year),i+1)==(2026,10):
                final=date(2026,10,17)
            if final<=cutoff:
                rows.append({'month':f'{year}-{i+1:02}', 'pnl':pnl, 'win_rate':record['win_rates'][i],
                             'through':str(final),'partial':final.day!=calendar.monthrange(final.year,final.month)[1]})
    return rows


def build_context(target,perspective='outlook',refresh=False):
    from .server import SERIES,TOPICS,fetch_series,summarize,news,correlate
    from .provenance import Ledger
    selected,start,end=forecast_window(target,perspective=perspective)
    ledger=Ledger()
    def get_series(key):
        if key=='cpi':return {'key':key,'label':SERIES[key][0],'points':[],'error':'Inflation vintages / release surprises are not loaded'}
        return fetch_series(key,start,end,refresh)
    with ThreadPoolExecutor(max_workers=8) as pool:series=list(pool.map(get_series,SERIES))
    btc=next(row for row in series if row['key']=='btc')
    def returns(points):
        return {d:v/p-1 for (prev,p),(d,v) in zip(points,points[1:]) if p and start.isoformat()<=d<=end.isoformat() and (date.fromisoformat(d)-date.fromisoformat(prev)).days==1}
    metrics=[]
    for row in series:
        if row['key']=='cpi':continue
        observation=summarize(row,start,end)
        ids=ledger.fetch(row,row['label'],available=observation.get('change') is not None)
        corr,n=correlate(returns(btc['points']),returns(row['points']))
        metrics.append({k:observation.get(k) for k in ['key','label','unit','change','last_date','coverage','observations','daily_volatility','error']} | {'btc_correlation':corr,'correlation_pairs':n,'source_ids':ids})
    with ThreadPoolExecutor(max_workers=5) as pool:
        feeds=list(pool.map(lambda key:news(key,start,end,refresh),TOPICS))
    publications=[]
    for feed in feeds:
        ledger.fetch(feed,feed['label']+' discovery','news_discovery',bool(feed.get('items')))
        for item in feed.get('items',[]):
            identifier=ledger.headline(item)
            publications.append({'topic':feed['label'],'title':item['title'][:300],'publisher':item['publisher'],
                                 'published_at':item['published_at'],'url':item['url'],'source_id':identifier})
    if sum(bool(row.get('points')) for row in series)<3:
        raise ValueError('Too little market data to describe sentiment; try again later')
    context={'report_format':FORMAT,'target_month':target,'perspective':perspective,
             'observation_start':str(start),'data_cutoff':str(end),
             'monthly_regimes':[{'start':str(start),'end':str(end),'metrics':metrics}],
             'headline_metadata_only':publications,'source_ledger':ledger.rows,
             'missing_news_topics':[f['label'] for f in feeds if f.get('error') or not f.get('items')],
             'missing_data':[{'label':row['label'],'reason':row.get('error','No observations')} for row in series if not row.get('points')],
             'limitations':'One observation month, with a prior close used only to calculate returns. Latest/revised prices, not point-in-time vintages. Headlines only, not full articles. No reliable inflation surprises, funding, options positioning or complete event calendar. Sentiment interpretation only; no strategy evaluation.',
             'reconstruction':(next_month(selected)<=now_utc().date().replace(day=1)) if perspective=='monitor' else selected<=now_utc().date().replace(day=1)}
    return context,sorted({row['title'] for row in ledger.rows if row['available']})


def usage_cost(usage,model):
    # Public list pricing verified 2026-10-21: Haiku 4.5 $1 input/$5 output per MTok.
    if not model.startswith('claude-haiku-4-5'):
        return None
    return round((usage.get('input_tokens',0)+5*usage.get('output_tokens',0))/1_000_000,6)


def ask_model(context,config):
    if config.get('provider')=='codex':
        from .codex_model import infer
        return infer(context,config)
    if config.get('provider')=='gemini':
        from .gemini_model import infer
        return infer(context,config)
    is_pairs=context.get('kind')=='pairs'
    payload={'model':config['model'], 'max_tokens':18000 if is_pairs else 3500, 'temperature':0.2, 'system':PAIR_SYSTEM if is_pairs else SYSTEM,
             'messages':[{'role':'user','content':'Produce the monthly verdict from this evidence JSON:\n'+json.dumps(context,ensure_ascii=False)}]}
    request=Request('https://api.anthropic.com/v1/messages',data=json.dumps(payload).encode(),method='POST',headers={
        'Content-Type':'application/json','anthropic-version':'2023-06-01','x-api-key':config['api_key']})
    try:
        with urlopen(request,timeout=90) as response:
            result=json.loads(response.read(1_000_000))
    except HTTPError as exc:
        # Do not include provider bodies/headers or secrets in browser or logs.
        reason={401:'API key was rejected',402:'API balance is unavailable',403:'API access was denied',404:'The configured Haiku model is unavailable',429:'Anthropic rate limit or credit limit reached'}.get(exc.code,'Anthropic request failed')
        raise ValueError(f'{reason} (HTTP {exc.code}); no analysis was archived') from None
    except (URLError,TimeoutError):
        raise ValueError('Anthropic could not be reached; no analysis was archived (a timed-out request may still be billed)') from None
    if result.get('stop_reason')=='max_tokens':
        raise ValueError('The AI response was truncated; no analysis was archived')
    text='\n'.join(block.get('text','') for block in result.get('content',[]) if block.get('type')=='text')
    parsed=validate_pair_output(text,context['pairs'],context.get('source_ledger',[]),context.get('recent_news')) if is_pairs else validate_output(text,context.get('source_ledger',[]))
    return parsed,result.get('usage',{}),result.get('model',config['model'])


@contextmanager
def database():
    LOCAL_DIR.mkdir(exist_ok=True)
    db=sqlite3.connect(LOCAL_DIR/'analyses.sqlite3',timeout=20)
    try:
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS analyses (id TEXT PRIMARY KEY, target_month TEXT NOT NULL, generated_at TEXT NOT NULL, document TEXT NOT NULL)')
            yield db
    finally:
        db.close()


def archive_list(target=None,kind=None,perspective=None):
    with database() as db:
        if target:
            rows=db.execute('SELECT document FROM analyses WHERE target_month=? ORDER BY generated_at DESC',(target,)).fetchall()
        else:
            rows=db.execute('SELECT document FROM analyses ORDER BY target_month DESC,generated_at DESC').fetchall()
    keys=('id','target_month','generated_at','data_cutoff','model','reconstruction')
    documents=[json.loads(row[0]) for row in rows]
    return [{**{k:j[k] for k in keys}, 'kind':j.get('kind','macro'),
             'symbols':[p.get('symbol') for p in j.get('analysis',{}).get('pairs',[]) if p.get('symbol')],
             'perspective':j.get('perspective','outlook'),
             'macro_analysis_id':j.get('macro_analysis_id'), 'estimated_cost_usd':j.get('estimated_cost_usd'),
             'report_format':j.get('report_format','legacy-strategy'), 'sentiment':j['analysis'].get('sentiment'), 'score':j['analysis'].get('score'), 'verdict':j['analysis'].get('verdict'), 'confidence':j['analysis'].get('confidence')} for j in documents
            if (kind is None or j.get('kind','macro')==kind) and (perspective is None or j.get('perspective','outlook')==perspective)]


def archive_get(identifier):
    with database() as db:
        row=db.execute('SELECT document FROM analyses WHERE id=?',(identifier,)).fetchone()
    if row is None:
        raise ValueError('Archived analysis was not found')
    return json.loads(row[0])


def unchanged_analysis(existing,context,config):
    if not existing:return None
    previous=archive_get(existing[0]['id'])
    same_model=previous.get('requested_model',previous['model'])==config['model'] and previous.get('provider','anthropic')==config.get('provider','anthropic')
    def stable(value):
        if isinstance(value,dict):return {k:stable(v) for k,v in value.items() if k not in ('retrieved_at','access')}
        if isinstance(value,list):return [stable(v) for v in value]
        return value
    if same_model and stable(previous.get('input_snapshot'))==stable(context):
        return {**previous,'reused_unchanged':True}
    return None


def generate(target,regenerate=False,perspective='outlook'):
    forecast_window(target,perspective=perspective)
    # Serialize generation to avoid a double click or multiple tabs billing twice.
    if not GENERATION_LOCK.acquire(blocking=False):
        raise ValueError('An analysis is already generating; wait for it to finish')
    try:
        existing=[d for d in archive_list(target,kind='macro',perspective=perspective) if d['report_format']==FORMAT]
        if existing and not regenerate:
            return archive_get(existing[0]['id'])
        config=settings()
        require_key(config)
        context,sources=build_context(target,perspective=perspective,refresh=regenerate)
        reused=None if regenerate else unchanged_analysis(existing,context,config)
        if reused:return reused
        summary,usage,model=ask_model(context,config)
        document={'id':uuid.uuid4().hex,'target_month':target,'kind':'macro','report_format':FORMAT,'perspective':perspective,'generated_at':now_utc().isoformat(),
                  'data_cutoff':context['data_cutoff'],'observation_start':context['observation_start'],
                  'model':model,'provider':config.get('provider','anthropic'),'requested_model':config['model'],'reconstruction':context['reconstruction'],'analysis':summary,
                  'sources':sources,'source_ledger':context.get('source_ledger',[]),'usage':usage,'estimated_cost_usd':usage_cost(usage,model),'input_snapshot':context}
        with database() as db:
            db.execute('INSERT INTO analyses VALUES (?,?,?,?)',(document['id'],target,document['generated_at'],json.dumps(document,ensure_ascii=False,allow_nan=False)))
        return document
    finally:
        GENERATION_LOCK.release()


def generate_pairs(target,macro_id,regenerate=False,perspective='outlook',symbol=None):
    from .pairs import build_pair_context,configured_pairs
    forecast_window(target,perspective=perspective)
    if not GENERATION_LOCK.acquire(blocking=False):
        raise ValueError('An analysis is already generating; wait for it to finish')
    try:
        macro=archive_get(macro_id)
        if macro.get('report_format')!=FORMAT or macro.get('kind','macro')!='macro' or macro['target_month']!=target or macro.get('perspective','outlook')!=perspective:
            raise ValueError('Choose a macro analysis for this same month and perspective first')
        existing=[d for d in archive_list(target,kind='pairs',perspective=perspective) if d['report_format']==FORMAT]
        if not regenerate:
            wanted=set(configured_pairs()) if symbol is None else {symbol}
            for archived in existing:
                if archived['macro_analysis_id']!=macro_id or not wanted.issubset(set(archived.get('symbols',[]))):
                    continue
                saved=archive_get(archived['id'])
                if wanted.issubset({row.get('symbol') for row in saved.get('input_snapshot',{}).get('pairs',[])}):
                    return saved
        config=settings()
        require_key(config)
        context,sources=build_pair_context(target,macro,perspective,refresh=regenerate,symbol=symbol)
        reused=None if regenerate else unchanged_analysis(existing,context,config)
        if reused:return reused
        summary,usage,model=ask_model(context,config)
        document={'id':uuid.uuid4().hex,'target_month':target,'kind':'pairs','report_format':FORMAT,'perspective':perspective,
                  'macro_analysis_id':macro_id,'generated_at':now_utc().isoformat(),'data_cutoff':context['data_cutoff'],
                  'observation_start':context['observation_start'],'model':model,'provider':config.get('provider','anthropic'),'requested_model':config['model'],'reconstruction':context['reconstruction'],
                  'analysis':summary,'sources':sources,'source_ledger':context.get('source_ledger',[]),'usage':usage,'estimated_cost_usd':usage_cost(usage,model),'input_snapshot':context}
        with database() as db:
            db.execute('INSERT INTO analyses VALUES (?,?,?,?)',(document['id'],target,document['generated_at'],json.dumps(document,ensure_ascii=False,allow_nan=False)))
        return document
    finally:
        GENERATION_LOCK.release()
