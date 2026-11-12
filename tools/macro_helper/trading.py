"""Manual Binance Futures TESTNET desk. Credentials never leave this process."""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from . import analysis, trades
from .pairs import ROOT, FAVORITES

BASE_URL = 'https://demo-fapi.binance.com'
LOCK = threading.RLock()
PREFIX = 'rh-'
# The desk trades USDT-M favorites only; USDC-M contracts belong to the crypto bot.
MARKET_QUOTE = 'USDT'
LIMIT_ACTIONS = ('limit_buy','limit_sell')
MAX_LIMITS = 3  # working limits per contract
TIMEFRAMES = ('15m','30m','1h','4h')
# action: order type, client ID suffix, ATR multiple field
PROTECTION = {'stop':('STOP_MARKET','-sl','stop_atr'),'target':('TAKE_PROFIT_MARKET','-tp','target_atr')}
DEFAULT = {'symbol':'BTCUSDT','timeframe':'30m','risk':50,'stop_atr':2.5,
           'target_atr':4,'adjust_risk':10,
           'keys':{'buy':'E','sell':'S','topup':'T','reduce':'R'}}


class ExchangeError(ValueError):
    def __init__(self, message, code=None, uncertain=False):
        super().__init__(message)
        self.code, self.uncertain = code, uncertain


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def dec(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite(): raise InvalidOperation
        return result
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('A finite numeric value is required') from None


def positive(value, name, maximum=1000000):
    result = dec(value)
    if not 0 < result <= maximum: raise ValueError(f'{name} must be greater than zero and at most {maximum}')
    return result


def fmt(value):
    return format(dec(value), 'f')


def rounded(value, step, up=False):
    return (dec(value)/dec(step)).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN)*dec(step)


def credentials():
    from dotenv import dotenv_values
    saved = dotenv_values(ROOT/'ggg.env', interpolate=False)
    names = ('BINANCE_TEST_FUTURES_API_KEY','BINANCE_TEST_FUTURES_SECRET_KEY')
    values = tuple(os.environ.get(key) or saved.get(key) for key in names)
    if not all(values): raise ValueError('Futures testnet credentials are missing in the crypto project’s ggg.env')
    return values


# Testnet exchangeInfo is a large, slow response (often 20–30 s); contract filters change rarely.
INFO_TTL = 6*3600
INFO_CACHE = {}
INFO_LOCK = threading.Lock()
CLOCK = {}
SYNC_LOCK = threading.Lock()
SYNC_SECONDS = 15


class Client:
    def __init__(self, public=False):
        self.key, self.secret = (None, None) if public else credentials()
        self.offset = 0
        self.opener = build_opener(NoRedirect())
        if public: return
        if time.time()-CLOCK.get('at',0)>600: self.sync_clock()
        self.offset = CLOCK['offset']

    def sync_clock(self):
        CLOCK.update(at=time.time(),offset=int(self.request('GET','/fapi/v1/time',signed=False)['serverTime'])-int(time.time()*1000))
        self.offset = CLOCK['offset']

    def request(self, method, path, params=None, signed=True, retried=False):
        data = dict(params or {})
        headers = {}
        if signed:
            data.update(timestamp=int(time.time()*1000)+self.offset,recvWindow=5000)
            headers['X-MBX-APIKEY'] = self.key
        query = urlencode(data)
        if signed:
            query += '&signature='+hmac.new(self.secret.encode(),query.encode(),hashlib.sha256).hexdigest()
        request = Request(BASE_URL+path+('?' + query if query else ''),method=method,headers=headers)
        try:
            with self.opener.open(request,timeout=15) as response:
                return json.load(response)
        except HTTPError as exc:
            try: body=json.loads(exc.read())
            except (ValueError, OSError): body={}
            code=body.get('code')
            # -1021: rejected before processing because the local clock drifted; resync and resend once.
            if code==-1021 and signed and not retried:
                self.sync_clock()
                return self.request(method,path,params,signed,retried=True)
            message=str(body.get('msg','Request rejected'))[:240]
            for secret in (self.key,self.secret):
                if secret: message=message.replace(secret,'[redacted]')
            raise ExchangeError(f'Binance testnet ({code or exc.code}): {message}',code,
                                method!='GET' and (exc.code>=500 or exc.code==408 or code in (-1006,-1007))) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise ExchangeError('Binance testnet did not respond; refresh account state before retrying.',uncertain=method!='GET') from None


@contextmanager
def database():
    analysis.LOCAL_DIR.mkdir(exist_ok=True)
    db=sqlite3.connect(analysis.LOCAL_DIR/'trading.sqlite3',timeout=20)
    try:
        with db:
            db.row_factory=sqlite3.Row
            db.execute('CREATE TABLE IF NOT EXISTS configs (name TEXT PRIMARY KEY, body TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, symbol TEXT, action TEXT, state TEXT, body TEXT, result TEXT, created REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS fill_sync (symbol TEXT PRIMARY KEY, since INTEGER NOT NULL, active INTEGER NOT NULL)')
            yield db
    finally: db.close()


def config(data):
    result={**DEFAULT,**{k:v for k,v in data.items() if k in DEFAULT}}
    if not isinstance(result['symbol'],str) or not re.fullmatch(r'[A-Z0-9]{3,25}',result['symbol']): raise ValueError('Choose a valid contract')
    if result['timeframe'] not in TIMEFRAMES: raise ValueError('Choose an ATR timeframe')
    for field in ('risk','stop_atr','target_atr','adjust_risk'):
        result[field]=float(positive(result[field],field,100 if 'atr' in field else 1000000))
    keys=result['keys']
    if not isinstance(keys,dict) or set(keys)!=set(DEFAULT['keys']): raise ValueError('Specify all four shortcuts')
    keys={k:str(v).upper() for k,v in keys.items()}
    if any(not re.fullmatch('[A-Z]',v) for v in keys.values()) or len(set(keys.values()))!=4:
        raise ValueError('Shortcuts must be four different letters')
    result['keys']=keys
    return result


def saved_config(row):
    body=json.loads(row['body'])
    # A configuration saved on a retired timeframe loads with the default one.
    if body.get('timeframe') not in TIMEFRAMES: body['timeframe']=DEFAULT['timeframe']
    return {'name':row['name'],**body}


def configs():
    with database() as db:
        return {'defaults':DEFAULT,'configs':[saved_config(r) for r in db.execute('SELECT * FROM configs ORDER BY name COLLATE NOCASE')]}


def save_config(data):
    name=str(data.get('name','')).strip()
    if not 1<=len(name)<=60: raise ValueError('Give this configuration a name (1–60 characters)')
    with LOCK,database() as db:
        if data.get('delete') is True: db.execute('DELETE FROM configs WHERE name=?',(name,))
        else: db.execute('INSERT OR REPLACE INTO configs VALUES (?,?)',(name,json.dumps(config(data))))
    return configs()


def exchange_info(client=None):
    with INFO_LOCK:
        if time.time()-INFO_CACHE.get('at',0)<INFO_TTL: return INFO_CACHE['data']
        path=analysis.LOCAL_DIR/'exchange_info.json'
        try:
            stored=json.loads(path.read_text(encoding='utf-8'))
            if time.time()-stored['at']<INFO_TTL: INFO_CACHE.update(stored); return stored['data']
        except (OSError, ValueError, KeyError, TypeError): pass
        info=(client or Client(public=True)).request('GET','/fapi/v1/exchangeInfo',signed=False)
        keep=('symbol','status','contractType','quoteAsset','marginAsset','filters')
        data={'symbols':[{k:s.get(k) for k in keep} for s in info['symbols'] if s.get('quoteAsset')==MARKET_QUOTE]}
        INFO_CACHE.update(at=time.time(),data=data)
        try:
            analysis.LOCAL_DIR.mkdir(exist_ok=True); path.write_text(json.dumps(INFO_CACHE),encoding='utf-8')
        except OSError: pass
        return data


def parallel(*calls):
    with ThreadPoolExecutor(len(calls)) as pool:
        return [f.result() for f in [pool.submit(call) for call in calls]]


def warm_up():
    # Fetch contract metadata in the background so the first desk refresh is fast.
    def run():
        try: exchange_info()
        except ValueError: pass
    threading.Thread(target=run,name='desk-warm-up',daemon=True).start()
    start_sync()


def market_info(client,symbol):
    info=exchange_info(client)
    matches=[s for s in info['symbols'] if s['symbol']==symbol and s['status']=='TRADING' and s['contractType']=='PERPETUAL' and s['quoteAsset']==MARKET_QUOTE]
    if not matches: raise ValueError('This contract is not available on Binance Futures testnet')
    return matches[0]


def quantity(info,raw,price,reduce=False,limit=False):
    filters={f['filterType']:f for f in info['filters']}
    lot=filters['LOT_SIZE']; market=lot if limit else filters.get('MARKET_LOT_SIZE',lot)
    steps=[dec(f['stepSize']) for f in (lot,market) if dec(f['stepSize'])>0]
    # Binance steps are decimal powers; enforce both filters explicitly.
    q=rounded(raw,max(steps))
    for f in (lot,market):
        if q<dec(f['minQty']) or q>dec(f['maxQty']) or (dec(f['stepSize'])>0 and q%dec(f['stepSize'])):
            raise ValueError('Requested size is outside the exchange quantity limits; adjust the risk amount')
    minimum=dec(filters.get('MIN_NOTIONAL',{}).get('notional',0))
    if q<=0 or (not reduce and q*price<minimum): raise ValueError('Risk amount is below the minimum tradable size')
    return q


def atr(candles,now_ms,period=14):
    closed=[c for c in candles if int(c[6])<now_ms]
    if len(closed)<period+1: raise ValueError('Not enough completed candles for ATR (14)')
    ranges=[max(dec(c[2])-dec(c[3]),abs(dec(c[2])-dec(p[4])),abs(dec(c[3])-dec(p[4]))) for p,c in zip(closed,closed[1:])]
    value=sum(ranges[:period])/period
    for item in ranges[period:]: value=(value*(period-1)+item)/period
    return positive(value,'ATR')


def positions(client,symbol=None):
    return [p for p in client.request('GET','/fapi/v3/positionRisk',{'symbol':symbol} if symbol else {}) if dec(p['positionAmt'])!=0]


def algos(client,symbol):
    rows=client.request('GET','/fapi/v1/openAlgoOrders',{'symbol':symbol})
    return rows if isinstance(rows,list) else rows.get('orders',[])


def is_true(value):
    return value is True or value=='true'


def protective_stop(position,orders):
    amount=dec(position['positionAmt']); side='SELL' if amount>0 else 'BUY'
    stops=[o for o in orders if o.get('orderType',o.get('type'))=='STOP_MARKET' and o.get('side')==side
           and o.get('positionSide','BOTH')==position.get('positionSide','BOTH')
           and (is_true(o.get('closePosition')) or (is_true(o.get('reduceOnly')) and dec(o.get('quantity',0))>=abs(amount)))]
    prices={dec(o['triggerPrice']) for o in stops}
    if len(prices)!=1: raise ValueError('No single full-position stop; top-up and reduce size by risk ÷ (stop ATR × ATR).')
    return next((o for o in stops if is_true(o.get('closePosition'))),stops[0])


def snapshot():
    client=Client()
    with ThreadPoolExecutor(4) as pool:
        balances,mode,info,open_positions=[f.result() for f in (pool.submit(client.request,'GET','/fapi/v3/balance'),
            pool.submit(client.request,'GET','/fapi/v1/positionSide/dual'),pool.submit(exchange_info,client),pool.submit(positions,client))]
        open_positions=[p for p in open_positions if p['symbol'].endswith(MARKET_QUOTE)]
        orders=dict(zip([p['symbol'] for p in open_positions],pool.map(lambda p: algos(client,p['symbol']),open_positions)))
    rows=[]
    for p in open_positions:
        amount=dec(p['positionAmt']); row={'symbol':p['symbol'],'side':'LONG' if amount>0 else 'SHORT','position_side':p.get('positionSide','BOTH'),
            'quantity':fmt(abs(amount)),'entry':p['entryPrice'],'mark':p['markPrice'],'pnl':p['unRealizedProfit'],'stop':None,'risk':None,'protected':bool(orders[p['symbol']]),
            'target':next((o['triggerPrice'] for o in orders[p['symbol']] if o.get('orderType',o.get('type'))=='TAKE_PROFIT_MARKET'),None)}
        try:
            stop=protective_stop(p,orders[p['symbol']])
            row['stop']=stop['triggerPrice']
            row['risk']=fmt(max(Decimal(0),(dec(p['entryPrice'])-dec(row['stop']))*(1 if amount>0 else -1))*abs(amount))
        except ValueError as exc: row['risk_note']=str(exc)
        rows.append(row)
    notices=check_limits(client)
    start_sync()
    with database() as db:
        pending=[{'id':r['id'],'symbol':r['symbol'],'action':r['action']} for r in db.execute("SELECT * FROM operations WHERE state IN ('running','unknown')")]
        working=[{'id':r['id'],'symbol':r['symbol'],'side':json.loads(r['body'])['side'],'price':json.loads(r['body'])['price'],'quantity':json.loads(r['body'])['quantity'],
                  'filled':json.loads(r['result'] or '{}').get('filled','0')}
                 for r in db.execute("SELECT * FROM operations WHERE state='working' ORDER BY created")]
    if working: ensure_watcher()
    return {'environment':'testnet','connected':True,'hedge_mode':is_true(mode.get('dualSidePosition')),
            'balances':[{k:b.get(k,'0') for k in ('asset','balance','availableBalance')} for b in balances if b['asset']==MARKET_QUOTE],
            'symbols':[f for f in FAVORITES if f in {s['symbol'] for s in info['symbols'] if s['status']=='TRADING' and s['contractType']=='PERPETUAL' and s['quoteAsset']==MARKET_QUOTE}],
            'limits':working,'notices':notices,'positions':rows,'pending':pending,'asof':time.time()}


def watch(symbol,since_ms):
    with database() as db:
        db.execute('INSERT INTO fill_sync VALUES (?,?,1) ON CONFLICT(symbol) DO UPDATE SET active=1,since=min(since,excluded.since)',(symbol,int(since_ms)))


def start_sync():
    # Archiving fills takes several slow testnet calls per contract; never make status wait for it.
    def run():
        try: sync_loop()
        finally: SYNC_LOCK.release()
    if SYNC_LOCK.acquire(blocking=False): threading.Thread(target=run,name='desk-fill-sync',daemon=True).start()


def sync_loop():
    # Runs without the page open, so triggered stops and manual closes reach the archive within seconds.
    while True:
        try:
            if not sync_fills(Client()): return
        except Exception: pass
        time.sleep(SYNC_SECONDS)


def sync_fills(client):
    """Archive every fill on watched USDT contracts, including orders placed or closed outside the desk.

    Returns whether any contract still needs watching."""
    started=time.time()
    with database() as db:
        if not db.execute('SELECT 1 FROM fill_sync').fetchone():
            for r in db.execute('SELECT symbol,min(created) FROM operations GROUP BY symbol').fetchall():
                db.execute('INSERT INTO fill_sync VALUES (?,?,1)',(r[0],int(r[1]*1000)-60000))
        if db.execute('PRAGMA user_version').fetchone()[0]<1:
            # Earlier versions dropped fills placed outside the desk; rescan what Binance still retains.
            db.execute('UPDATE fill_sync SET active=1,since=coalesce((SELECT CAST(min(created)*1000 AS INTEGER)-60000 FROM operations WHERE symbol=fill_sync.symbol),since)')
            db.execute('PRAGMA user_version=1')
        watched=[r for r in db.execute('SELECT symbol,since FROM fill_sync WHERE active=1') if r[0].endswith(MARKET_QUOTE)]
        actions={r['id'][:28]:r['action'] for r in db.execute('SELECT id,action FROM operations')}
        busy={r[0] for r in db.execute("SELECT symbol FROM operations WHERE state IN ('working','running','unknown')")}
    now=int(time.time()*1000)+client.offset
    remaining=False
    for symbol,since in watched:
        # One slow or failing contract must not hold back the others.
        try: remaining|=not sync_symbol(client,symbol,max(since,now-7*86400000+60000),actions,busy,started)
        except ValueError: remaining=True
    return remaining


def sync_symbol(client,symbol,since,actions,busy,started):
    # Check flatness before fetching fills so a flat account guarantees its closing fill is fetched below.
    flat=symbol not in busy and not positions(client,symbol) and not any(str(a.get('clientAlgoId','')).startswith(PREFIX) for a in algos(client,symbol))
    orders={o['orderId']:o for o in client.request('GET','/fapi/v1/allOrders',{'symbol':symbol,'startTime':since})}
    events={}
    for o in orders.values():
        cid=o.get('clientOrderId','')
        if cid.startswith(PREFIX): events[o['orderId']]={'buy':'open','sell':'open','limit_buy':'open','limit_sell':'open','topup':'fill','reduce':'partial_close'}.get(actions.get(cid[len(PREFIX):],''),'close')
    for a in client.request('GET','/fapi/v1/allAlgoOrders',{'symbol':symbol}):
        if str(a.get('clientAlgoId','')).startswith(PREFIX) and a.get('actualOrderId'): events[int(a['actualOrderId'])]='close'
    fills=client.request('GET','/fapi/v1/userTrades',{'symbol':symbol,'startTime':since,'limit':1000})
    rows=[]
    for f in fills:
        order=orders.get(f['orderId'],{}); event=events.get(f['orderId'],'fill')
        # Outside closes fold by quantity; reduce-only also catches a breakeven close with zero realized PnL.
        if event in ('open','fill') and (is_true(order.get('reduceOnly')) or is_true(order.get('closePosition')) or dec(f['realizedPnl'])): event='partial_close'
        key=hashlib.sha256(f'binance-testnet-desk:{symbol}:{f["id"]}'.encode()).hexdigest()
        rows.append((key,'executed',symbol,event,f['side'].lower(),
                     datetime.fromtimestamp(f['time']/1000,timezone.utc).isoformat(),
                     float(f['qty']),float(f['price']),float(f['realizedPnl']) if event!='open' else None,None,
                     float(f['commission']),f.get('commissionAsset',''),'Binance testnet desk',key,json.dumps(f,sort_keys=True),analysis.now_utc().isoformat()))
    with analysis.database() as db:
        trades.init_tables(db)
        db.executemany('INSERT OR IGNORE INTO trade_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',rows)
    if not flat: return False
    # Stop watching once the position, its protection and working limits are gone, unless an order started meanwhile.
    with database() as db:
        db.execute('UPDATE fill_sync SET active=0,since=? WHERE symbol=? AND NOT EXISTS (SELECT 1 FROM operations WHERE symbol=? AND created>=?)',
                   (max([f['time'] for f in fills],default=since)+1,symbol,symbol,started))
    return True


def market_order(client,symbol,side,q,request_id,reduce=False):
    params={'symbol':symbol,'side':side,'type':'MARKET','quantity':fmt(q),'newClientOrderId':PREFIX+request_id[:28],'newOrderRespType':'RESULT'}
    if reduce: params['reduceOnly']='true'
    try: order=client.request('POST','/fapi/v1/order',params)
    except ExchangeError as exc:
        if not exc.uncertain: raise
        try: order=client.request('GET','/fapi/v1/order',{'symbol':symbol,'origClientOrderId':params['newClientOrderId']})
        except ValueError:
            raise ExchangeError('Order outcome is unknown. Use Reconcile before any further entry; do not repeat this order.',uncertain=True) from None
    if order.get('status') not in ('FILLED','CANCELED','EXPIRED','REJECTED'):
        raise ExchangeError('Order is still pending. Use Reconcile to check its outcome.',uncertain=True)
    # The submit response can omit avgPrice; the order query always carries it.
    if dec(order.get('executedQty') or 0)>0 and dec(order.get('avgPrice') or 0)<=0:
        order=client.request('GET','/fapi/v1/order',{'symbol':symbol,'origClientOrderId':params['newClientOrderId']})
    return order


def bracket(client,symbol,side,price,kind,client_id):
    params={'symbol':symbol,'side':side,'algoType':'CONDITIONAL','type':kind,
            'triggerPrice':fmt(price),'closePosition':'true','workingType':'MARK_PRICE','clientAlgoId':client_id}
    # The same ID can be used to recover an ambiguous protection request.
    try: return client.request('POST','/fapi/v1/algoOrder',params)
    except ExchangeError as exc:
        if exc.uncertain or exc.code in (-4116,-4111):
            try: return client.request('GET','/fapi/v1/algoOrder',{'clientAlgoId':client_id})
            except ValueError: pass
        raise


def cleanup(client,symbol):
    warnings=[]
    for o in algos(client,symbol):
        if str(o.get('clientAlgoId','')).startswith(PREFIX):
            try: client.request('DELETE','/fapi/v1/algoOrder',{'algoId':o['algoId']})
            except ValueError: warnings.append('A desk protection order could not be canceled; check Binance before reopening this contract.')
    return warnings


def protect_entry(client,plan,order,request_id):
    symbol=plan['symbol']; side=plan['side']; direction=1 if side=='BUY' else -1
    if dec(order.get('executedQty',0))<=0: return {'message':'No quantity filled; no position was opened.','order':order}
    entry=positive(order.get('avgPrice',0),'Fill price'); tick=dec(plan['tick']); distance=dec(plan['distance']); target=dec(plan['target_distance'])
    remaining=positions(client,symbol)
    if not remaining:
        return {'message':'The entry filled but the position is already closed.','warnings':cleanup(client,symbol)}
    p=remaining[0]
    if len(remaining)!=1 or dec(p['positionAmt'])*direction<=0 or abs(dec(p['positionAmt']))>dec(order['executedQty']) or abs(dec(p['entryPrice'])-entry)>tick:
        raise ExchangeError('The position changed outside this desk; protection recovery needs a fresh account check.',uncertain=True)
    stop=rounded(entry-direction*distance,tick,up=direction>0)
    take=rounded(entry+direction*target,tick,up=direction<0)
    if stop<=0 or take<=0 or stop==entry or take==entry: raise ValueError('Invalid protection price after fill')
    exit_side='SELL' if direction>0 else 'BUY'
    existing={o.get('clientAlgoId') for o in algos(client,symbol)}
    for level,kind,suffix in ((stop,'STOP_MARKET','-sl'),(take,'TAKE_PROFIT_MARKET','-tp')):
        client_id=PREFIX+request_id[:24]+suffix
        if client_id not in existing: bracket(client,symbol,exit_side,level,kind,client_id)
    return {'message':'Market entry filled; stop-loss and take-profit are active on testnet.','quantity':order['executedQty'],'entry':fmt(entry),'stop':fmt(stop),'target':fmt(take)}


def limit_contract(symbol):
    if symbol not in FAVORITES: raise ValueError('Limit entries are limited to your favorite USDT contracts')
    return symbol


def limit_order(client,symbol,side,q,price,request_id,reduce=False):
    params={'symbol':symbol,'side':side,'type':'LIMIT','timeInForce':'GTX','quantity':fmt(q),'price':fmt(price),
            'newClientOrderId':PREFIX+request_id[:28],'newOrderRespType':'RESULT'}
    if reduce: params['reduceOnly']='true'
    try: return client.request('POST','/fapi/v1/order',params)
    except ExchangeError as exc:
        if exc.code==-5022: raise ExchangeError('The post-only limit would have filled immediately as a taker, so Binance rejected it; nothing was opened.',exc.code) from None
        if not exc.uncertain: raise
        try: return client.request('GET','/fapi/v1/order',{'symbol':symbol,'origClientOrderId':params['newClientOrderId']})
        except ValueError:
            raise ExchangeError('Limit order outcome is unknown. Use Reconcile; it will not be resubmitted.',uncertain=True) from None


def place_limit(data,request_id):
    with LOCK:
        with database() as db:
            old=db.execute('SELECT * FROM operations WHERE id=?',(request_id,)).fetchone()
            if old:
                if old['state'] in ('done','working'): return json.loads(old['result'])
                raise ValueError('This action was already submitted. Refresh or reconcile its outcome; it will not be sent twice.')
            if db.execute("SELECT 1 FROM operations WHERE state IN ('running','unknown')").fetchone():
                raise ValueError('Reconcile the pending order before increasing exposure')
        symbol=limit_contract(str(data.get('symbol',''))); cfg=config(data)
        side='BUY' if data['action']=='limit_buy' else 'SELL'
        client=Client()
        if is_true(client.request('GET','/fapi/v1/positionSide/dual').get('dualSidePosition')):
            raise ValueError('This desk requires Binance one-way position mode; it does not change your account mode automatically')
        info=market_info(client,symbol)
        if info['quoteAsset']!=MARKET_QUOTE: raise ValueError('Limit entries use USDT contracts')
        current,protection,working=parallel(lambda: positions(client,symbol),lambda: algos(client,symbol),
                                            lambda: client.request('GET','/fapi/v1/openOrders',{'symbol':symbol}))
        # Stacked limits are for unprotected manual positions; a stop/target means manage it below.
        if protection: raise ValueError(f'{symbol} has a stop or target; manage the existing position below')
        if len(working)>=MAX_LIMITS: raise ValueError(f'{symbol} already has {MAX_LIMITS} working limits; manage the existing position or cancel one')
        amount=dec(current[0]['positionAmt']) if current else Decimal(0)
        reduce=amount*(1 if side=='BUY' else -1)<0
        book=client.request('GET','/fapi/v1/ticker/bookTicker',{'symbol':symbol},signed=False)
        bid=positive(book['bidPrice'],'Bid price'); ask=positive(book['askPrice'],'Ask price')
        tick=next(f['tickSize'] for f in info['filters'] if f['filterType']=='PRICE_FILTER')
        raw=data.get('limit_price')
        # A blank price joins the best bid (buy) or ask (sell) as a maker.
        price=(bid if side=='BUY' else ask) if raw in (None,'') else rounded(positive(raw,'Limit price'),tick,up=side=='SELL')
        if (side=='BUY' and price>=ask) or (side=='SELL' and price<=bid):
            raise ValueError('A post-only limit must rest below the ask (buy) or above the bid (sell)')
        candles=client.request('GET','/fapi/v1/klines',{'symbol':symbol,'interval':cfg['timeframe'],'limit':100},signed=False)
        volatility=atr(candles,int(time.time()*1000)+client.offset)
        # Plain manual entry: no stop or target is placed; size is still risk / (stop ATR distance).
        distance=volatility*dec(cfg['stop_atr'])
        if distance<dec(tick): raise ValueError('The stop ATR multiplier is too small to size this limit')
        raw=dec(cfg['risk'])/distance
        q=quantity(info,min(raw,abs(amount)) if reduce else raw,price,reduce,limit=True)
        balance=client.request('GET','/fapi/v3/balance')
        available=next((dec(b['availableBalance']) for b in balance if b['asset']==info['marginAsset']),Decimal(0))
        leverage=select_leverage(client,symbol,q*price)
        if not reduce and q*price/leverage>available: raise ValueError(f'Insufficient available testnet {info["marginAsset"]} margin for this risk and leverage')
        plan={'symbol':symbol,'action':data['action'],'side':side,'quantity':fmt(q),'price':fmt(price),'tick':tick,'leverage':leverage,'reduce':reduce}
        with database() as db:
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?)',(request_id,symbol,data['action'],'running',json.dumps(plan),'{}',time.time()))
        watch(symbol,(time.time()-60)*1000+client.offset)
        try:
            # Only the first order sets leverage; an open position or stacked limits keep theirs.
            if not amount and not working: client.request('POST','/fapi/v1/leverage',{'symbol':symbol,'leverage':leverage})
            plan['market_submitted']=True
            with database() as db: db.execute('UPDATE operations SET body=? WHERE id=?',(json.dumps(plan),request_id))
            order=limit_order(client,symbol,side,q,price,request_id,reduce)
        except ValueError as exc:
            uncertain=isinstance(exc,ExchangeError) and exc.uncertain
            record(request_id,'unknown' if uncertain else 'failed',{'error':str(exc)})
            raise ValueError(str(exc)) from None
        if order.get('status') in ('EXPIRED','REJECTED','CANCELED') and dec(order.get('executedQty') or 0)<=0:
            result={'message':'The post-only limit would have filled immediately as a taker and was canceled; nothing was opened.'}
            record(request_id,'done',result); return result
        result={'message':f'Post-only limit {side.lower()} {fmt(q)} {symbol} at {fmt(price)} is working; no stop or target is placed.','filled':order.get('executedQty','0')}
        record(request_id,'working',result)
    ensure_watcher()
    notices=check_limits(client)
    return {**result,'warnings':notices} if notices else result


def check_limits(client):
    notices=[]
    with LOCK:
        with database() as db: rows=list(db.execute("SELECT * FROM operations WHERE state='working'"))
        for row in rows:
            plan=json.loads(row['body']); symbol=row['symbol']
            try:
                order=client.request('GET','/fapi/v1/order',{'symbol':symbol,'origClientOrderId':PREFIX+row['id'][:28]})
                filled=dec(order.get('executedQty') or 0); status=order.get('status')
                if status in ('FILLED','CANCELED','EXPIRED','REJECTED'):
                    if filled>0: text=f'{symbol} limit filled {fmt(filled)} at {plan["price"]}; no stop or target is set.'
                    else:
                        text=f'{symbol} limit {status.lower()} without a fill.'
                    record(row['id'],'done',{'message':text,'filled':fmt(filled)}); notices.append(text)
                else:
                    with database() as db: db.execute('UPDATE operations SET result=? WHERE id=?',(json.dumps({**json.loads(row['result'] or '{}'),'filled':fmt(filled)}),row['id']))
            except ExchangeError as exc:
                if exc.uncertain: record(row['id'],'unknown',{'error':str(exc)})
                notices.append(str(exc))
            except ValueError as exc: notices.append(f'{symbol} limit check: {exc}')
    return notices


def cancel_limit(data):
    identifier=str(data.get('limit_id',''))
    with LOCK:
        with database() as db: row=db.execute("SELECT * FROM operations WHERE id=? AND state='working'",(identifier,)).fetchone()
        if not row: raise ValueError('This limit is no longer working; refresh the account')
        client=Client()
        try: client.request('DELETE','/fapi/v1/order',{'symbol':row['symbol'],'origClientOrderId':PREFIX+row['id'][:28]})
        except ExchangeError as exc:
            if exc.code!=-2011: raise  # -2011: already filled or canceled; the check below settles it
        notices=check_limits(client)
    return {'message':' '.join(notices) or 'Cancel requested; refresh to confirm.'}


WATCHER=None


def ensure_watcher():
    global WATCHER
    with LOCK:
        if WATCHER is None or not WATCHER.is_alive():
            WATCHER=threading.Thread(target=watch_limits,name='desk-limit-watcher',daemon=True); WATCHER.start()


def watch_limits():
    # Settles working limits (filled / canceled) while the helper runs.
    while True:
        with database() as db:
            if not db.execute("SELECT 1 FROM operations WHERE state='working'").fetchone(): return
        try: check_limits(Client())
        except Exception: pass
        time.sleep(5)


def select_leverage(client,symbol,notional):
    # Mirrors the crypto bot: highest leverage whose bracket cap (less 5%) fits the notional.
    resp=client.request('GET','/fapi/v1/leverageBracket',{'symbol':symbol})
    items=resp if isinstance(resp,list) else [resp]
    brackets=next((sorted(i['brackets'],key=lambda b:b['bracket']) for i in items if i.get('symbol')==symbol and i.get('brackets')),None)
    if not brackets: raise ValueError('Leverage brackets unavailable for this contract')
    for b in brackets:
        if notional<=dec(b['notionalCap'])*Decimal('0.95'): return int(b['initialLeverage'])
    return int(brackets[-1]['initialLeverage'])


def finish_entry(client,plan,order,request_id):
    try: return protect_entry(client,plan,order,request_id)
    except ValueError:
        # Fail closed: an entry without both protections is not considered successful.
        try:
            remaining=positions(client,plan['symbol'])
            for p in remaining:
                amount=dec(p['positionAmt'])
                direction=1 if plan['side']=='BUY' else -1
                if amount*direction<=0 or abs(amount)>dec(order.get('executedQty',0)) or abs(dec(p['entryPrice'])-dec(order.get('avgPrice',0)))>dec(plan['tick']):
                    raise ValueError('External position change; do not close an unrelated position')
                market_order(client,plan['symbol'],'SELL' if amount>0 else 'BUY',abs(amount),request_id[:24]+'exit',True)
            if positions(client,plan['symbol']): raise ValueError('Position remains open')
            warnings=cleanup(client,plan['symbol'])
        except ValueError:
            raise ExchangeError('Protection failed and emergency close could not be confirmed. Check the position immediately; close it on Binance or use Reconcile.',uncertain=True) from None
        return {'message':'Protection failed; the new position was emergency-closed at market.','warning':True,'warnings':warnings}


def record(request_id,state,result):
    with database() as db: db.execute('UPDATE operations SET state=?,result=? WHERE id=?',(state,json.dumps(result),request_id))


def set_protection(data,request_id):
    # Adds one close-position stop or target to an open position, such as a filled limit entry.
    action=data['action']; symbol=str(data.get('symbol','')); kind,suffix,multiple=PROTECTION[action]
    with LOCK:
        with database() as db:
            old=db.execute('SELECT * FROM operations WHERE id=?',(request_id,)).fetchone()
            if old:
                if old['state']=='done': return json.loads(old['result'])
                raise ValueError('This action was already submitted. Refresh or reconcile its outcome; it will not be sent twice.')
        cfg=config(data); client=Client(); info=market_info(client,symbol)
        mode,current,orders,candles=parallel(lambda: client.request('GET','/fapi/v1/positionSide/dual'),lambda: positions(client,symbol),lambda: algos(client,symbol),
            lambda: client.request('GET','/fapi/v1/klines',{'symbol':symbol,'interval':cfg['timeframe'],'limit':100},signed=False))
        if is_true(mode.get('dualSidePosition')):
            raise ValueError('This desk requires Binance one-way position mode; it does not change your account mode automatically')
        if len(current)!=1: raise ValueError('Selected position no longer exists; refresh positions')
        p=current[0]; direction=1 if dec(p['positionAmt'])>0 else -1
        if data.get('position_side')!=('LONG' if direction>0 else 'SHORT'):
            raise ValueError('Position direction changed; select the current position again')
        exit_side='SELL' if direction>0 else 'BUY'
        # Binance allows one close-position order per type and side, so nothing is replaced here.
        if any(o.get('orderType',o.get('type'))==kind and o.get('side')==exit_side for o in orders):
            raise ValueError(f'{symbol} already has a {action}; cancel it on Binance before setting a new one')
        tick=next(f['tickSize'] for f in info['filters'] if f['filterType']=='PRICE_FILTER')
        distance=atr(candles,int(time.time()*1000)+client.offset)*dec(cfg[multiple])
        if distance<dec(tick): raise ValueError('The ATR multiplier is too small for this contract')
        # Anchored to the average entry like a market entry's protection, rounded toward it.
        sign=-direction if action=='stop' else direction
        price=rounded(dec(p['entryPrice'])+sign*distance,tick,up=sign<0); mark=dec(p['markPrice']); anchor='entry'
        # A stop the market has already crossed from entry is re-measured from the mark instead.
        if action=='stop' and (price-mark)*sign<=0:
            price=rounded(mark+sign*distance,tick,up=sign<0); anchor='mark'
        if price<=0 or (price-mark)*sign<=0:
            raise ValueError(f'A {action} at {fmt(price)} is already past the mark price {fmt(mark)}; choose a different ATR multiple')
        client_id=PREFIX+request_id[:24]+suffix
        plan={'symbol':symbol,'action':action,'side':exit_side,'price':fmt(price),'client_id':client_id}
        with database() as db:
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?)',(request_id,symbol,action,'running',json.dumps(plan),'{}',time.time()))
        watch(symbol,(time.time()-60)*1000+client.offset)
        try: bracket(client,symbol,exit_side,price,kind,client_id)
        except ValueError as exc:
            uncertain=isinstance(exc,ExchangeError) and exc.uncertain
            record(request_id,'unknown' if uncertain else 'failed',{'error':str(exc)})
            raise ValueError(str(exc)) from None
        result={'message':f'{"Stop-loss" if action=="stop" else "Take-profit"} active at {fmt(price)} on testnet{" (measured from the mark price, which is already beyond an entry-based stop)" if anchor=="mark" else ""}; it closes the whole position when triggered.','price':fmt(price)}
        record(request_id,'done',result)
        return result


def execute(data):
    request_id=str(data.get('request_id',''))
    if not re.fullmatch('[a-f0-9]{32}',request_id): raise ValueError('A unique request ID is required')
    action=data.get('action'); symbol=str(data.get('symbol',''))
    if action in LIMIT_ACTIONS: return place_limit(data,request_id)
    if action in PROTECTION: return set_protection(data,request_id)
    if action not in ('buy','sell','topup','reduce'): raise ValueError('Unknown trading action')
    with LOCK:
        with database() as db:
            old=db.execute('SELECT * FROM operations WHERE id=?',(request_id,)).fetchone()
            if old:
                if old['state']=='done': return json.loads(old['result'])
                raise ValueError('This action was already submitted. Refresh or reconcile its outcome; it will not be sent twice.')
            if action in ('buy','sell','topup') and db.execute("SELECT 1 FROM operations WHERE state IN ('running','unknown')").fetchone():
                raise ValueError('Reconcile the pending order before increasing exposure')
        client=Client(); info=market_info(client,symbol)
        # Independent preflight reads run together; each testnet round trip takes about a second.
        mode,current,book=parallel(lambda: client.request('GET','/fapi/v1/positionSide/dual'),lambda: positions(client,symbol),
                                   lambda: client.request('GET','/fapi/v1/ticker/bookTicker',{'symbol':symbol},signed=False))
        if is_true(mode.get('dualSidePosition')):
            raise ValueError('This desk requires Binance one-way position mode; it does not change your account mode automatically')
        plan={'symbol':symbol,'action':action}
        if action in ('buy','sell'):
            if info['quoteAsset']!=MARKET_QUOTE: raise ValueError('Market entries use USDT contracts')
            if symbol not in FAVORITES: raise ValueError('Market entries are limited to your favorite contracts')
            if current: raise ValueError('This contract already has a position; select it to top up, reduce, or close')
            cfg=config(data); side='BUY' if action=='buy' else 'SELL'
            pending_algos,pending_orders,candles,balance=parallel(lambda: algos(client,symbol),lambda: client.request('GET','/fapi/v1/openOrders',{'symbol':symbol}),
                lambda: client.request('GET','/fapi/v1/klines',{'symbol':symbol,'interval':cfg['timeframe'],'limit':100},signed=False),
                lambda: client.request('GET','/fapi/v3/balance'))
            # Do not adopt stale protection or pre-existing entry orders.
            if pending_algos or pending_orders:
                raise ValueError('This contract has outstanding orders; clear them on Binance before opening a new desk position')
            volatility=atr(candles,int(time.time()*1000)+client.offset)
            price=positive(book['askPrice' if side=='BUY' else 'bidPrice'],'Market price')
            distance=volatility*dec(cfg['stop_atr']); target=volatility*dec(cfg['target_atr'])
            tick=next(f['tickSize'] for f in info['filters'] if f['filterType']=='PRICE_FILTER')
            if min(distance,target)<dec(tick) or (side=='BUY' and price<=distance) or (side=='SELL' and price<=target):
                raise ValueError('ATR multipliers produce invalid stop or target prices')
            q=quantity(info,dec(cfg['risk'])/distance,price)
            available=next((dec(b['availableBalance']) for b in balance if b['asset']==info['marginAsset']),Decimal(0))
            leverage=select_leverage(client,symbol,q*price)
            if q*price/leverage>available: raise ValueError('Insufficient available testnet margin for this risk and leverage')
            plan.update(side=side,quantity=fmt(q),distance=fmt(distance),target_distance=fmt(target),tick=tick,leverage=leverage)
        else:
            if len(current)!=1: raise ValueError('Selected position no longer exists; refresh positions')
            p=current[0]; amount=dec(p['positionAmt']); direction=1 if amount>0 else -1
            if data.get('position_side')!=('LONG' if direction>0 else 'SHORT'):
                raise ValueError('Position direction changed; select the current position again')
            side=('BUY' if direction>0 else 'SELL') if action=='topup' else ('SELL' if direction>0 else 'BUY')
            price=positive(book['askPrice' if side=='BUY' else 'bidPrice'],'Market price')
            try: stop=protective_stop(p,algos(client,symbol))
            except ValueError: stop=None
            if stop:
                trigger=dec(stop['triggerPrice'])
                # Add risk at the new fill; remove risk measured from the existing entry.
                basis=price if action=='topup' else dec(p['entryPrice'])
                distance=(basis-trigger)*direction
                if distance<=0: raise ValueError('The stop locks in profit or has been crossed; dollar-risk resizing is undefined. Close it on Binance.')
                plan.update(stop=fmt(trigger),promote_stop=action=='topup' and not is_true(stop.get('closePosition')),stop_working_type=stop.get('workingType','MARK_PRICE'))
            else:
                # No stop to measure from: size like an entry, risk / (stop ATR x ATR).
                cfg=config(data)
                candles=client.request('GET','/fapi/v1/klines',{'symbol':symbol,'interval':cfg['timeframe'],'limit':100},signed=False)
                distance=atr(candles,int(time.time()*1000)+client.offset)*dec(cfg['stop_atr'])
            risk=positive(data.get('adjust_risk'),'Risk adjustment')
            raw=risk/distance
            if action=='reduce' and raw>=abs(amount): raise ValueError('Reduction reaches the full position risk; close it on Binance instead')
            q=quantity(info,raw,price,action=='reduce')
            plan.update(risk_change=fmt(q*distance))
            plan.update(side=side,quantity=fmt(q))
        # Durable claim before the first mutation; never automatically repeat a market order.
        with database() as db:
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?)',(request_id,symbol,action,'running',json.dumps(plan),'{}',time.time()))
        watch(symbol,(time.time()-60)*1000+client.offset)
        try:
            if action in ('buy','sell'):
                client.request('POST','/fapi/v1/leverage',{'symbol':symbol,'leverage':plan['leverage']})
            if plan.get('promote_stop'):
                if plan['stop_working_type']!='MARK_PRICE': raise ValueError('Top-up requires an existing mark-price stop; its trigger basis will not be changed')
                bracket(client,symbol,'SELL' if plan['side']=='BUY' else 'BUY',plan['stop'],'STOP_MARKET',PREFIX+request_id[:24]+'-sl')
            # Check again after preflight; external bots may also be using the account.
            latest=positions(client,symbol)
            if action in ('buy','sell') and latest: raise ValueError('A position appeared during preparation; entry canceled')
            if action not in ('buy','sell') and (len(latest)!=1 or dec(latest[0]['positionAmt'])!=amount):
                raise ValueError('Position size changed during preparation; refresh before retrying')
            plan['market_submitted']=True
            with database() as db: db.execute('UPDATE operations SET body=? WHERE id=?',(json.dumps(plan),request_id))
            order=market_order(client,symbol,plan['side'],dec(plan['quantity']),request_id,action=='reduce')
            if action in ('buy','sell'): result=finish_entry(client,plan,order,request_id)
            else:
                result={'message':f'{action.capitalize()} order finished on testnet.','quantity':order.get('executedQty','0'),'order_status':order.get('status'),'risk_change_estimate':plan.get('risk_change')}
                if not positions(client,symbol): result['warnings']=cleanup(client,symbol)
            record(request_id,'done',result)
            return result
        except Exception as exc:
            uncertain=isinstance(exc,ExchangeError) and exc.uncertain
            message=str(exc) if isinstance(exc,ValueError) else 'Trading action interrupted. Reconcile the outcome before retrying.'
            record(request_id,'unknown' if uncertain or not isinstance(exc,ValueError) else 'failed',{'error':message})
            raise ValueError(message) from None


def reconcile():
    results=[]
    with LOCK:
        client=Client()
        with database() as db: rows=list(db.execute("SELECT * FROM operations WHERE state IN ('running','unknown')"))
        for row in rows:
            plan=json.loads(row['body'])
            try:
                if row['action'] in PROTECTION:
                    active=plan['client_id'] in {o.get('clientAlgoId') for o in algos(client,row['symbol'])}
                    result={'message':f'The {row["action"]} is active on Binance.' if active else f'The {row["action"]} is not active on Binance; set it again if the position is still open.'}
                    record(row['id'],'done',result);results.append(result);continue
                if not plan.get('market_submitted'):
                    result={'message':'Preparation was interrupted before an order was sent. No order will be replayed.'}
                    if row['action'] in LIMIT_ACTIONS and not positions(client,row['symbol']): result['warnings']=cleanup(client,row['symbol'])
                    record(row['id'],'done',result);results.append(result);continue
                order=client.request('GET','/fapi/v1/order',{'symbol':row['symbol'],'origClientOrderId':PREFIX+row['id'][:28]})
                if row['action'] in LIMIT_ACTIONS:
                    record(row['id'],'working',{'filled':order.get('executedQty','0')})
                    results.append({'message':'Limit order found on Binance; it is now tracked as a working limit.'});continue
                if order.get('status') not in ('FILLED','CANCELED','EXPIRED','REJECTED'): continue
                if row['action'] in ('buy','sell') and positions(client,row['symbol']):
                    result=finish_entry(client,plan,order,row['id'])
                else:
                    result={'message':'Order outcome reconciled.','order_status':order['status']}
                    if not positions(client,row['symbol']): result['warnings']=cleanup(client,row['symbol'])
                record(row['id'],'done',result); results.append(result)
            except ValueError as exc:
                # After the request's receive window and transport timeout, a definitive
                # not-found response permits a new user action, never an automatic replay.
                if isinstance(exc,ExchangeError) and exc.code==-2013 and time.time()-row['created']>60:
                    result={'message':'Binance confirms no matching order after its receive window. Refresh before placing a new order.'}
                    if row['action'] in LIMIT_ACTIONS and not positions(client,row['symbol']): result['warnings']=cleanup(client,row['symbol'])
                    record(row['id'],'done',result);results.append(result)
                else: results.append({'error':str(exc),'request_id':row['id']})
        results+=[{'message':n} for n in check_limits(client)]
    return {'results':results,'message':'Reconciliation checked; unresolved orders remain blocked.'}
