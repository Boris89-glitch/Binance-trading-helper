"""Local monthly regime research. No exchange credentials or trading imports."""
import argparse
import calendar
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
from pathlib import Path
import statistics
import sys
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CACHE_DIR = ROOT / '.macro_helper_cache'
LOCK = threading.Lock()
CACHE = {}
RESULTS = {
    '2025': {'overall_win_rate': 40.5, 'reported_pnl': 9814, 'win_rates': [41.8,42.8,41.7,39.2,45.2,37.2,44.0,41.0,38.8,40.5,37.1,37.7], 'pnl': [1445,1651,1452,110,3717,-1199,2811,1074,-221,808,-933,-901]},
    '2026': {'overall_win_rate': 40.1, 'reported_pnl': 5211, 'win_rates': [42.1,43.3,39.8,41.9,37.9,34.9,39.2,40.3,38.7,45.2], 'pnl': [1939,2502,368,1571,-800,-2526,-28,556,-237,1867]},
}
# Price proxies are named accurately: DXY is not the Fed's trade-weighted dollar;
# front-month WTI futures are not spot oil; ETH/BTC is not total altcoin breadth.
SERIES = {
    'btc': ('Bitcoin / USDT', 'binance', 'BTCUSDT', 'percent', 'crypto'),
    'eth': ('Ether / USDT', 'binance', 'ETHUSDT', 'percent', 'crypto'),
    'sol': ('Solana / USDT', 'binance', 'SOLUSDT', 'percent', 'crypto'),
    'ethbtc': ('ETH / BTC rotation proxy', 'binance', 'ETHBTC', 'percent', 'rotation'),
    'spx': ('S&P 500', 'yahoo', '^GSPC', 'percent', 'correlation'),
    'nasdaq': ('Nasdaq Composite', 'yahoo', '^IXIC', 'percent', 'correlation'),
    'nikkei': ('Nikkei 225', 'yahoo', '^N225', 'percent', 'asia'),
    'hangseng': ('Hang Seng', 'yahoo', '^HSI', 'percent', 'asia'),
    'dollar': ('ICE US Dollar Index (DXY)', 'yahoo', 'DX-Y.NYB', 'percent', 'dollar'),
    'yield': ('US 10-year yield', 'yahoo', '^TNX', 'bp', 'rates'),
    'oil': ('WTI front-month futures', 'yahoo', 'CL=F', 'percent', 'oil'),
    'vix': ('Cboe VIX', 'yahoo', '^VIX', 'points', 'volatility'),
    'cpi': ('US CPI, seasonally adjusted (latest revised)', 'fred', 'CPIAUCSL', 'percent', 'inflation'),
}
TOPICS = {
    'policy': ('Dollar, rates & inflation', '(site:federalreserve.gov OR site:bls.gov OR site:bea.gov) (inflation OR monetary OR interest)'),
    'asia': ('Asia & cross-market policy', '(site:boj.or.jp OR site:hkma.gov.hk OR site:pbc.gov.cn OR site:imf.org) (policy OR market OR China OR Japan)'),
    'oil': ('Oil & geopolitical transmission', '(site:eia.gov OR site:iea.org OR site:imf.org) (oil OR energy OR supply OR geopolitical)'),
    'geopolitics': ('Geopolitics: documented actions', '(site:un.org OR site:consilium.europa.eu OR site:ofac.treasury.gov) (sanctions OR conflict OR shipping OR trade)'),
    'flows': ('Liquidity, rotations & positioning', '(site:bis.org OR site:insights.deribit.com OR site:blog.coinshares.com) (liquidity OR flows OR options OR bitcoin)'),
}
NEWS_FILTERS = {
    'policy': (('federalreserve.gov','bls.gov','bea.gov'), ('inflation','monetary','interest','rates','cpi','pce','employment','price index','fomc','gdp','economic')),
    'asia': (('boj.or.jp','hkma.gov.hk','pbc.gov.cn','imf.org'), ('monetary','interest','policy','yen','yuan','rates','econom','market','liquid','inflation','growth','tariff','export')),
    'oil': (('eia.gov','iea.org','imf.org'), ('oil','petroleum','energy','gas','supply','geopolit','opec')),
    'geopolitics': (('un.org','consilium.europa.eu','ofac.treasury.gov'), ('sanction','conflict','shipping','trade','tariff','hormuz','red sea','supply','oil','energy')),
    'flows': (('bis.org','insights.deribit.com','blog.coinshares.com'), ('liquid','flows','option','bitcoin','crypto','financial condition','position','volatil','funding')),
}
SOURCES = [
    ('FOMC decisions & meeting dates', 'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm'),
    ('CPI release calendar', 'https://www.bls.gov/schedule/news_release/cpi.htm'),
    ('Employment release calendar', 'https://www.bls.gov/schedule/news_release/empsit.htm'),
    ('BEA release calendar: PCE & GDP', 'https://www.bea.gov/news/schedule'),
    ('Bank of Japan meeting schedule', 'https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm'),
    ('EIA petroleum reports', 'https://www.eia.gov/petroleum/supply/weekly/'),
    ('Deribit expiry contract policy', 'https://support.deribit.com/hc/en-us/articles/25944688876957'),
    ('Deribit settlement mechanics', 'https://support.deribit.com/hc/en-us/articles/29734325712413-Settlement'),
]


def get_url(url):
    from .provenance import record
    req = Request(url, headers={'User-Agent': 'Mozilla/5.0 (MonthlyMacroResearch/1.0)'})
    try:
        with urlopen(req, timeout=16) as response:
            body=response.read(6_000_000).decode('utf-8-sig')
            record(url,True,response.geturl())
            return body
    except Exception:
        record(url,False)
        raise


def cached(key, fetch, refresh=False):
    """Persist successful fetches; never silently replace a failure with old data."""
    from .provenance import capture
    key='audit_v1_'+key
    with LOCK:
        entry = CACHE.get(key)
    path = CACHE_DIR / (key + '.json')
    if entry is None and path.exists():
        try:
            entry = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            pass
    if entry and not refresh and time.time() - entry['time'] < 6 * 3600:
        return {**entry['data'], 'retrieved_at': entry['retrieved_at'], 'cached': True}
    fetched = datetime.now(timezone.utc).isoformat()
    try:
        with capture() as requests:
            data = fetch()
        data={**data,'request_log':requests}
        entry = {'time': time.time(), 'retrieved_at': fetched, 'data': data}
        with LOCK:
            CACHE[key] = entry
            CACHE_DIR.mkdir(exist_ok=True)
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(entry, allow_nan=False), encoding='utf-8')
            temp.replace(path)
        return {**data, 'retrieved_at': fetched, 'cached': False}
    except Exception as exc:
        return {'error': f'{type(exc).__name__}: {str(exc)[:180]}', 'retrieved_at': fetched, 'points': [],'request_log':requests}


def fetch_series(key, start, end, refresh=False):
    label, provider, symbol, unit, topic = SERIES[key]
    source = (f'https://www.binance.com/en/trade/{symbol}' if provider == 'binance' else
              f'https://finance.yahoo.com/quote/{symbol}/history/' if provider == 'yahoo' else
              f'https://fred.stlouisfed.org/series/{symbol}')

    def fetch():
        lower = datetime.combine(start - timedelta(days=10), datetime.min.time(), timezone.utc)
        upper = datetime.combine(end + timedelta(days=1), datetime.min.time(), timezone.utc)
        if provider == 'binance':
            query = urlencode({'symbol': symbol, 'interval': '1d', 'startTime': int(lower.timestamp()*1000), 'endTime': int(upper.timestamp()*1000)-1, 'limit': 1000})
            rows = json.loads(get_url('https://api.binance.com/api/v3/klines?' + query))
            if not isinstance(rows, list):
                raise ValueError('Exchange did not return candle data')
            now_ms = time.time()*1000
            points = [[datetime.fromtimestamp(row[0]/1000, timezone.utc).date().isoformat(), float(row[4])] for row in rows if row[6] < min(now_ms, upper.timestamp()*1000)]
        elif provider == 'yahoo':
            from urllib.parse import quote
            query = urlencode({'period1': int(lower.timestamp()), 'period2': int(upper.timestamp()), 'interval': '1d'})
            body = json.loads(get_url(f'https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe="")}?{query}'))
            result = body['chart']['result'][0]
            values = result['indicators']['quote'][0]['close']
            # Exchange calendar dates, not UTC conversion (Tokyo opens on prior UTC day).
            offset = result['meta'].get('gmtoffset', 0)
            points = [[datetime.fromtimestamp(ts+offset, timezone.utc).date().isoformat(), float(value)] for ts, value in zip(result.get('timestamp', []), values) if value is not None]
        else:
            url = 'https://fred.stlouisfed.org/graph/graph.csv?' + urlencode({'id': symbol, 'cosd': (start-timedelta(days=100)).isoformat(), 'coed': end.isoformat()})
            reader = csv.DictReader(io.StringIO(get_url(url)))
            if not reader.fieldnames or symbol not in reader.fieldnames:
                raise ValueError('FRED returned no CSV (possibly maintenance); inflation is unavailable')
            points = [[row[reader.fieldnames[0]], float(row[symbol])] for row in reader if row[symbol] not in ('.', '')]
        points = sorted({day: value for day, value in points if math.isfinite(value) and day <= end.isoformat()}.items())
        if not points:
            raise ValueError('No observations in provider response')
        return {'points': points}

    result = cached(f'{key}_{start}_{end}', fetch, refresh)
    return {'key': key, 'label': label, 'unit': unit, 'topic': topic, 'source': source, 'provider': provider, **result}


def summarize(series, start, end):
    points = series.get('points', [])
    before = [(d,v) for d,v in points if d < start.isoformat()]
    inside = [(d,v) for d,v in points if start.isoformat() <= d <= end.isoformat()]
    result = {**series, 'change': None, 'correlation': None, 'n_pairs': 0}
    if not before or not inside:
        return {**result, 'coverage': 'Missing baseline or observations for this window'}
    baseline = before[-1]
    last = inside[-1]
    delta = last[1]-baseline[1]
    change = delta*100 if series['unit'] == 'bp' else delta if series['unit'] == 'points' else delta/baseline[1]*100 if baseline[1] else None
    gap = (date.fromisoformat(last[0])-date.fromisoformat(baseline[0])).days
    daily = returns(points, start, end)
    values = list(daily.values())
    efficiency = abs(delta)/sum(abs(b-a) for (_,a),(_,b) in zip([baseline]+inside, inside)) if len(inside) and any(v != baseline[1] for _,v in inside) else 0
    result.update(change=change, baseline_date=baseline[0], last_date=last[0], last_value=last[1], observations=len(inside), days=gap,
                  coverage='partial / lagged' if (end-date.fromisoformat(last[0])).days > (40 if series['key']=='cpi' else 4) or (start-date.fromisoformat(baseline[0])).days > 7 else 'available',
                  efficiency=efficiency, daily_volatility=statistics.stdev(values)*100 if len(values)>1 else None)
    return result


def returns(points, start, end):
    """No forward filling; holiday gaps must not become fake zero returns."""
    return {day: (value/prior-1) for (_,prior),(day,value) in zip(points, points[1:]) if prior and start.isoformat() <= day <= end.isoformat()}


def correlate(a, b):
    keys = sorted(a.keys() & b.keys())
    if len(keys)<10:
        return None, len(keys)
    x,y = [a[k] for k in keys], [b[k] for k in keys]
    if not statistics.pstdev(x) or not statistics.pstdev(y):
        return None, len(keys)
    return statistics.correlation(x,y), len(keys)


def news(topic, start, end, refresh=False):
    label, query = TOPICS[topic]
    def fetch():
        query_string = f'{query} after:{start-timedelta(days=1)} before:{end+timedelta(days=1)}'
        url = 'https://news.google.com/rss/search?' + urlencode({'q': query_string, 'hl':'en-US', 'gl':'US', 'ceid':'US:en'})
        root = ET.fromstring(get_url(url))
        items = []
        for node in root.findall('./channel/item'):
            try:
                published = parsedate_to_datetime(node.findtext('pubDate')).astimezone(timezone.utc)
            except (ValueError, TypeError):
                continue
            if not start <= published.date() <= end:
                continue
            source = node.find('source')
            link = node.findtext('link', '')
            publisher_url = source.get('url','') if source is not None else ''
            host = (urlparse(publisher_url).hostname or '').lower()
            domains, keywords = NEWS_FILTERS[topic]
            title = node.findtext('title','')
            if not any(host == domain or host.endswith('.'+domain) for domain in domains):
                continue
            if not any(word in title.lower() for word in keywords):
                continue
            if not link.startswith('https://'):
                continue
            items.append({'title': title, 'url': link, 'published_at': published.isoformat(), 'publisher': source.text if source is not None else 'Unknown publisher', 'publisher_url': publisher_url})
        return {'items': sorted(items, key=lambda x:x['published_at'], reverse=True)[:15]}
    return {'topic':topic, 'label':label, **cached(f'news_v2_{topic}_{start}_{end}', fetch, refresh)}


def evidence(series):
    by_key = {x['key']:x for x in series}
    def change(key): return by_key[key].get('change')
    observations = []
    btc = by_key['btc']
    if btc.get('change') is not None:
        e = btc['efficiency']
        observations.append({'topic':'Trend / breakout conditions', 'fact': f'BTC directional efficiency {e:.2f}; daily return volatility {btc["daily_volatility"] or 0:.2f}%. Efficiency is net movement / total absolute daily movement.', 'support': 'Higher efficiency is consistent with cleaner directional conditions for a breakout strategy.' if e>=0.35 else 'Low efficiency is consistent with reversals that can frustrate breakouts.', 'opposition':'Daily closes can hide profitable intraday trends; BTC alone does not represent the full traded basket. Test trade-level R and stop-outs.', 'keys':['btc']})
    for key, topic, support, opposite in [
        ('dollar','Dollar & rates','A rising dollar can tighten dollar-denominated financial conditions; a falling dollar can ease them.','Crypto can decouple; test the measured correlation and yields rather than assume the sign.'),
        ('ethbtc','Rotations','A rising ETH/BTC ratio means ETH outperformed BTC; a falling ratio means it underperformed.','This pair does not establish broad altcoin participation, capital inflows, or your basket’s profitability.'),
        ('nikkei','Asian markets','An equity move may indicate a shared risk regime; compare Hang Seng, FX and BTC co-movement.','Shared monthly direction does not show Asian markets led crypto. UTC/session alignment needs intraday data.'),
        ('oil','Oil & geopolitical transmission','Oil repricing is a measurable transmission channel for supply/inflation concerns.','Oil moves also reflect demand and contract rolls; a headline or monthly move cannot identify the cause.'),
        ('vix','Volatility & sentiment','VIX measures S&P 500 implied volatility and gives an observable risk-pricing proxy.','VIX is neither crypto implied volatility nor news sentiment. High fear can accompany profitable breakouts.'),
        ('cpi','Inflation','CPI is the realized price index, useful as macro context.','Latest revised index values are not the release surprise or a point-in-time historical vintage; do not backtest filters from them.'),
    ]:
        value = change(key)
        observations.append({'topic':topic, 'fact': f'{by_key[key]["label"]}: '+('unavailable for this window.' if value is None else f'{value:+.2f} {"bp" if by_key[key]["unit"]=="bp" else "points" if by_key[key]["unit"]=="points" else "%"} across available observations.'), 'support':support, 'opposition':opposite, 'keys':[key], 'available': value is not None})
    return observations


def parse_window(params):
    month = params.get('month',['2026-10'])[0]
    selected = datetime.strptime(month, '%Y-%m').date().replace(day=1)
    if selected.year not in (2025,2026):
        raise ValueError('Choose a month in 2025 or 2026')
    mode = params.get('mode',['review'])[0]
    if mode not in ('review','blind'):
        raise ValueError('Unknown research mode')
    if mode == 'blind':
        end = selected-timedelta(days=1)
        start = end.replace(day=1)
    else:
        start = selected
        end = date(selected.year, selected.month, calendar.monthrange(selected.year,selected.month)[1])
        # Match the supplied performance cutoff instead of comparing 17 days to 31.
        if month == '2026-10': end = date(2026,10,17)
    today = datetime.now(timezone.utc).date()
    end = min(end, today-timedelta(days=1))
    if end < start: raise ValueError('This window has no completed days yet')
    return month, mode, start, end


def monthly_report(params):
    month, mode, start, end = parse_window(params)
    refresh = params.get('refresh',['0'])[0] == '1'
    def load(key):
        if key == 'cpi' and mode == 'blind':
            return {'key':key, 'label':SERIES[key][0], 'unit':'percent', 'topic':'inflation', 'source':'https://fred.stlouisfed.org/series/CPIAUCSL', 'provider':'fred', 'points':[], 'change':None, 'correlation':None, 'n_pairs':0, 'error':'Excluded: reference-month CPI was not necessarily published by this cutoff; historical vintages are not loaded.', 'retrieved_at':datetime.now(timezone.utc).isoformat()}
        return summarize(fetch_series(key,start,end,refresh),start,end)
    with ThreadPoolExecutor(max_workers=8) as pool:
        series = list(pool.map(load, SERIES))
    btc = next(s for s in series if s['key']=='btc')
    btc_returns = returns(btc['points'],start,end)
    for item in series:
        if item['key'] not in ('btc','cpi'):
            # Compare only adjacent calendar days in both feeds. This drops holiday
            # and weekend gaps, avoiding US multi-day return vs BTC one-day return.
            def comparable(points):
                return {d:(v/p-1) for (prev,p),(d,v) in zip(points,points[1:]) if p and (date.fromisoformat(d)-date.fromisoformat(prev)).days==1 and start.isoformat()<=d<=end.isoformat()}
            item['correlation'],item['n_pairs'] = correlate(comparable(btc['points']),comparable(item['points']))
    expiry = date(start.year,start.month,calendar.monthrange(start.year,start.month)[1])
    expiry -= timedelta(days=(expiry.weekday()-4)%7)
    cards = evidence(series)
    if mode == 'review':
        idx = int(month[5:])-1
        observed = [(year, data['pnl'][idx]) for year,data in RESULTS.items() if len(data['pnl'])>idx]
        same_sign = len(observed)==2 and (observed[0][1]>0)==(observed[1][1]>0)
        cards.insert(0, {'topic':'Seasonality: same month, different years', 'fact':'; '.join(f'{year}: ${value:+,}' for year,value in observed)+('. October 2026 ends on the 17th, so windows differ.' if idx==9 else ''), 'support':'The supplied months share the same P&L sign. That is a candidate seasonal hypothesis.' if same_sign else 'The available monthly results do not establish a recurring same-sign pattern.', 'opposition':'Only two years (or one for November/December); correlated trades and untested explanations make calendar filters especially prone to overfitting. Validate on untouched years.', 'keys':[]})
        current=RESULTS[month[:4]]
        if len(current['pnl'])>idx and btc.get('change') is not None:
            pnl=current['pnl'][idx]
            eff=btc['efficiency']
            mismatch=(pnl>0 and eff<0.20) or (pnl<0 and eff>0.35)
            cards.insert(1, {'topic':'Strategy outcome vs BTC regime', 'fact':f'Supplied strategy P&L ${pnl:+,}; BTC daily trend efficiency {eff:.2f}.', 'support':'This is counterevidence to a simple claim that profitable months require high BTC daily trend efficiency.' if pnl>0 and eff<0.20 else 'Losses despite a directional BTC month challenge a simple claim that cleaner BTC trends ensure profitable breakouts.' if mismatch else 'These measurements can generate a regime hypothesis, but one monthly pairing does not validate it.', 'opposition':'Efficiency thresholds of 0.20 / 0.35 are exploratory heuristics. The strategy trades multiple coins and intraday paths; compare trade-level net R and matched regimes before changing filters.', 'keys':['btc']})
    return {'month':month, 'mode':mode, 'start':str(start), 'end':str(end), 'generated_at':datetime.now(timezone.utc).isoformat(), 'series':series, 'evidence':cards, 'expiry':str(expiry), 'expiry_in_window':start<=expiry<=end, 'sources':SOURCES}


class Handler(BaseHTTPRequestHandler):
    def valid_host(self):
        return self.headers.get('Host','').lower() in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}')

    def do_GET(self):
        if not self.valid_host():
            return self.json({'error':'Local access only'},403)
        parsed = urlparse(self.path)
        try:
            from . import analysis
            if parsed.path == '/api/trading/configs':
                from . import trading
                return self.json(trading.configs())
            if parsed.path == '/api/trading/status':
                from . import trading
                return self.json(trading.snapshot())
            if parsed.path in ('/trading.js','/trading.css'):
                return self.respond((HERE/parsed.path[1:]).read_bytes(),'text/javascript; charset=utf-8' if parsed.path.endswith('.js') else 'text/css; charset=utf-8')
            if parsed.path == '/api/ai/status':
                return self.json(analysis.settings_status())
            if parsed.path == '/api/pairs':
                from .pairs import configured_pairs,configured_instruments
                return self.json({'symbols':configured_pairs(),'instruments':configured_instruments()})
            if parsed.path in ('/api/trades','/api/trades/export'):
                from . import trades
                params=parse_qs(parsed.query)
                options={k:params.get(k,[None])[0] for k in ('mode','side','symbol','month')}
                if parsed.path.endswith('/export'):
                    return self.respond(trades.export_csv(**options).encode('utf-8-sig'),'text/csv; charset=utf-8')
                return self.json(trades.list_trades(**options,limit=params.get('limit',['50'])[0],offset=params.get('offset',['0'])[0]))
            if parsed.path == '/api/archive':
                return self.json({'analyses':analysis.archive_list(parse_qs(parsed.query).get('target',[None])[0])})
            if parsed.path.startswith('/api/archive/'):
                return self.json(analysis.archive_get(parsed.path.rsplit('/',1)[-1]))
            if parsed.path == '/':
                return self.respond((HERE/'index.html').read_bytes(),'text/html; charset=utf-8')
            if parsed.path == '/app.js':
                return self.respond((HERE/'app.js').read_bytes(),'text/javascript; charset=utf-8')
            if parsed.path == '/style.css':
                return self.respond((HERE/'style.css').read_bytes(),'text/css; charset=utf-8')
            if parsed.path == '/brief.css':
                return self.respond((HERE/'brief.css').read_bytes(),'text/css; charset=utf-8')
            if parsed.path == '/workspace.css':
                return self.respond((HERE/'workspace.css').read_bytes(),'text/css; charset=utf-8')
            if parsed.path == '/api/results':
                return self.json({'years':RESULTS, 'fixed_dollars_per_trade':20, 'starting_balance':1000, 'cutoff':'2026-10-17', 'provenance':'User-supplied rounded monthly aggregates; not revalidated against raw trade logs.'})
            if parsed.path == '/api/report':
                return self.json(monthly_report(parse_qs(parsed.query)))
            if parsed.path == '/api/news':
                params = parse_qs(parsed.query)
                _,_,start,end = parse_window(params)
                with ThreadPoolExecutor(max_workers=4) as pool:
                    feeds = list(pool.map(lambda key: news(key,start,end,params.get('refresh',['0'])[0]=='1'),TOPICS))
                return self.json({'start':str(start),'end':str(end),'feeds':feeds})
            self.json({'error':'Not found'},404)
        except (ValueError, KeyError) as exc:
            self.json({'error':str(exc)},400)
        except Exception:
            self.json({'error':'Research request failed; check the local terminal.'},500)
            import traceback
            traceback.print_exc()

    def do_POST(self):
        from . import analysis
        origin=self.headers.get('Origin')
        allowed=(f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}')
        if not self.valid_host() or (origin and origin not in allowed):
            return self.json({'error':'Local same-origin requests only'},403)
        if self.headers.get('Content-Type','').split(';')[0]!='application/json':
            return self.json({'error':'JSON requests only'},415)
        try:
            path=urlparse(self.path).path
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=32768:
                raise ValueError('Invalid request size')
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict):
                raise ValueError('Expected a JSON object')
            if path.startswith('/api/trading/'):
                if origin not in allowed:
                    return self.json({'error':'Trading requires an explicit local browser origin'},403)
                from . import trading
                if path=='/api/trading/configs': return self.json(trading.save_config(data))
                if path=='/api/trading/order': return self.json(trading.execute(data))
                if path=='/api/trading/reconcile': return self.json(trading.reconcile())
                if path=='/api/trading/cancel': return self.json(trading.cancel_limit(data))
            if path=='/api/ai/settings':
                return self.json(analysis.save_settings(data))
            if path=='/api/analysis':
                return self.json(analysis.generate(data.get('target_month'),data.get('regenerate') is True,data.get('perspective','outlook')))
            if path=='/api/analysis/pairs':
                return self.json(analysis.generate_pairs(data.get('target_month'),data.get('macro_id'),data.get('regenerate') is True,data.get('perspective','outlook'),data.get('symbol')))
            return self.json({'error':'Not found'},404)
        except (ValueError,TypeError) as exc:
            return self.json({'error':str(exc)},400)
        except Exception:
            return self.json({'error':'Analysis could not be completed; existing archives were preserved'},500)

    def respond(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def json(self, value, status=200):
        self.respond(json.dumps(value,allow_nan=False).encode(),'application/json; charset=utf-8',status)

    def log_message(self, fmt, *args):
        print(f'[macro helper] {fmt % args}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    print(f'Monthly macro helper: http://127.0.0.1:{args.port}  (type r + Enter to restart, q + Enter to quit)',flush=True)
    from . import trading
    trading.warm_up()
    restart=False
    def commands():
        nonlocal restart
        for line in sys.stdin:
            if line.strip().lower() in ('r','q'):
                restart=line.strip().lower()=='r'; server.shutdown(); return
    threading.Thread(target=commands,daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    if restart: sys.exit(3)  # start.bat relaunches on exit code 3
