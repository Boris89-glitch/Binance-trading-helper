"""Configured futures pairs and public, venue-matched research observations."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone, date
import json
from pathlib import Path
from urllib.parse import urlencode

PROJECT_ROOT=Path(__file__).resolve().parents[2]
ROOT=PROJECT_ROOT.parent/'crypto'


# The user's Binance USDT-M favorites (also the trading desk's market-order contracts).
FAVORITES = ('BTCUSDT','ETHUSDT','BNBUSDT','HYPEUSDT','TRXUSDT','ZECUSDT','DOGEUSDT','XMRUSDT','LINKUSDT',
             'XLMUSDT','BCHUSDT','UNIUSDT','AVAXUSDT','LTCUSDT','HBARUSDT','SUIUSDT','1000SHIBUSDT','XRPUSDT','ADAUSDT','SOLUSDT')


def base_asset(symbol):
    for quote in ('USDC','USDT'):
        if symbol.endswith(quote):return symbol[:-len(quote)].removeprefix('1000')
    return symbol


def configured_instruments():
    return [{'symbol':s,'label':s,'asset_class':'crypto','venue':'Binance USDT-M futures'} for s in FAVORITES]


def configured_pairs():
    instruments=configured_instruments()
    symbols=[i['symbol'] for i in instruments]
    if len(symbols)!=len(set(symbols)):
        raise ValueError('Configured instruments must have distinct symbols across markets')
    return symbols


def fetch_pair(symbol,start,end,refresh=False):
    instrument=next((i for i in configured_instruments() if i['symbol']==symbol),None)
    if instrument is None:raise ValueError('Unknown configured instrument')
    from .server import cached,get_url
    def fetch():
        lower=datetime.combine(start-timedelta(days=10),datetime.min.time(),timezone.utc)
        upper=datetime.combine(end+timedelta(days=1),datetime.min.time(),timezone.utc)
        query=urlencode({'symbol':symbol,'interval':'1d','startTime':int(lower.timestamp()*1000),'endTime':int(upper.timestamp()*1000)-1,'limit':1000})
        rows=json.loads(get_url('https://fapi.binance.com/fapi/v1/klines?'+query))
        if not isinstance(rows,list):
            raise ValueError('No futures candles returned for this configured pair')
        now=datetime.now(timezone.utc).timestamp()*1000
        points=[[datetime.fromtimestamp(r[0]/1000,timezone.utc).date().isoformat(),float(r[4])] for r in rows if r[6]<min(now,upper.timestamp()*1000)]
        if not points:
            raise ValueError('No completed futures candles available for this window')
        return {'points':points}
    return {'key':symbol,'label':symbol+' futures','unit':'percent','provider':instrument['venue'],'asset_class':'crypto',**cached(f'futures_{symbol}_{start}_{end}',fetch,refresh)}


def fetch_industry_news(symbol,instrument,start,end,refresh=False):
    """Collect dated headline metadata about this one coin."""
    from .server import cached,get_url
    from email.utils import parsedate_to_datetime
    from urllib.parse import urlencode
    import xml.etree.ElementTree as ET
    coin=base_asset(symbol)
    label=f'{coin} crypto news'
    query=f'{coin} (crypto OR cryptocurrency OR token OR blockchain)'
    def fetch():
        q=f'{query} after:{start-timedelta(days=1)} before:{end+timedelta(days=1)}'
        url='https://news.google.com/rss/search?'+urlencode({'q':q,'hl':'en-US','gl':'US','ceid':'US:en'})
        root=ET.fromstring(get_url(url));items=[]
        for node in root.findall('./channel/item'):
            try:published=parsedate_to_datetime(node.findtext('pubDate')).astimezone(timezone.utc)
            except (ValueError,TypeError,AttributeError):continue
            link=node.findtext('link','');source=node.find('source')
            if not start<=published.date()<=end or not link.startswith('https://'):continue
            items.append({'title':node.findtext('title','')[:300],'url':link,'published_at':published.isoformat(),
                          'publisher':source.text if source is not None else 'Unknown publisher',
                          'publisher_url':source.get('url','') if source is not None else ''})
        return {'items':sorted(items,key=lambda row:row['published_at'],reverse=True)[:15]}
    return {'topic':f'industry_{symbol.lower()}','label':label,'query':query,**cached(f'coin_news_{symbol}_{start}_{end}',fetch,refresh)}


def build_pair_context(target,macro,perspective,refresh=False,symbol=None):
    from .analysis import forecast_window,next_month
    from .server import fetch_series,summarize,correlate
    from .provenance import Ledger
    from .sentiment import FORMAT
    _,start,end=forecast_window(target,perspective=perspective)
    if macro['data_cutoff']!=str(end):
        raise ValueError('Refresh macro AI first so both analyses use the same data cutoff')
    symbols=configured_pairs()
    if symbol is not None:
        if symbol not in symbols:raise ValueError('Choose a configured coin or stock')
        symbols=[symbol]
    instruments={i['symbol']:i for i in configured_instruments()}
    ledger=Ledger(macro.get('source_ledger',[]))
    with ThreadPoolExecutor(max_workers=8) as pool:
        series=list(pool.map(lambda s:fetch_pair(s,start,end,refresh),symbols))
    btc=fetch_series('btc',start,end,refresh)
    ledger.fetch(btc,'Bitcoin reference price')
    def returns(points,first,last):
        return {d:v/p-1 for (prev,p),(d,v) in zip(points,points[1:]) if p and first.isoformat()<=d<=last.isoformat() and (date.fromisoformat(d)-date.fromisoformat(prev)).days==1}
    pairs=[]
    for s in series:
        windows=[]
        current=start
        while current<=end:
            last=min(next_month(current)-timedelta(days=1),end)
            observation=summarize(s,current,last)
            ids=ledger.fetch(s,instruments[s['key']]['label'],available=observation.get('change') is not None)
            corr,n=correlate(returns(btc['points'],current,last),returns(s.get('points',[]),current,last))
            windows.append({'month':current.strftime('%Y-%m'),'through':str(last),**{k:observation.get(k) for k in ('change','daily_volatility','observations','last_date','coverage','error')},'btc_correlation':corr,'correlation_pairs':n,'source_ids':ids})
            current=next_month(current)
        instrument=instruments[s['key']]
        pairs.append({'symbol':s['key'],'label':instrument['label'],'asset_class':instrument['asset_class'],
                      'venue':instrument['venue'],'trade_directions':'Long and short',
                      'windows':windows,'available':bool(windows and windows[-1]['change'] is not None and windows[-1]['coverage']=='available')})
    industry_feeds=[fetch_industry_news(s,instruments[s],start,end,refresh) for s in symbols]
    industry_headlines=[]
    for feed in industry_feeds:
        ledger.fetch(feed,feed['label']+' news discovery','news_discovery',bool(feed.get('items')))
        for item in feed.get('items',[]):
            source_id=ledger.headline(item)
            industry_headlines.append({'symbol':feed['topic'].removeprefix('industry_').upper(),'topic':feed['label'],**item,'source_id':source_id})
    from .recent_news import collect
    recent=collect(symbols,end,ledger,refresh)
    return {'kind':'pairs','report_format':FORMAT,'target_month':target,'perspective':perspective,'observation_start':str(start),'data_cutoff':str(end),
            'reconstruction':macro['reconstruction'],'macro_analysis_id':macro['id'],
            'macro_summary':macro['analysis'],'pairs':pairs,'industry_headline_metadata_only':industry_headlines,
            'missing_industry_news':[feed['label'] for feed in industry_feeds if feed.get('error') or not feed.get('items')],
            'recent_news':recent,
            'source_ledger':ledger.rows,
            'limitations':'One observation month for prices and monthly components; recent_news adds the last ~7 days of headlines for the news pulse only. Coin news is dated headline metadata only; article bodies were not fetched. No order book, funding, options positioning or token-unlock data. Describe sentiment only, without strategy claims.'}, sorted({row['title'] for row in ledger.rows if row['available']})
