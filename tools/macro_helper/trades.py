"""Trade-event archive only: no execution, inferred positions or paper fills."""
import csv
from datetime import datetime
import io


def init_tables(db):
    db.execute('''CREATE TABLE IF NOT EXISTS trade_events (
        id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('executed','paper')),
        symbol TEXT NOT NULL, event TEXT NOT NULL, side TEXT NOT NULL,
        occurred_at TEXT NOT NULL, quantity REAL NOT NULL, price REAL NOT NULL,
        reported_pnl REAL, reported_r REAL, fee REAL, fee_asset TEXT,
        source TEXT NOT NULL, source_key TEXT NOT NULL UNIQUE, raw TEXT NOT NULL,
        imported_at TEXT NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS trade_event_lookup ON trade_events(symbol,mode,occurred_at)')


def list_events(mode=None,symbol=None,month=None,limit=250,offset=0):
    from .analysis import database
    conditions=[];values=[]
    if mode:
        if mode not in ('executed','paper'):raise ValueError('Unknown trade mode')
        conditions.append('mode=?');values.append(mode)
    if symbol:conditions.append('symbol=?');values.append(symbol)
    if month:
        try:parsed=datetime.strptime(month,'%Y-%m')
        except (ValueError,TypeError):raise ValueError('Use YYYY-MM for the trade month') from None
        if parsed.strftime('%Y-%m')!=month:raise ValueError('Use YYYY-MM for the trade month')
        conditions.append('substr(occurred_at,1,7)=?');values.append(month)
    where=' WHERE '+' AND '.join(conditions) if conditions else ''
    try:limit,offset=int(limit),int(offset)
    except (ValueError,TypeError):raise ValueError('Page limit and offset must be whole numbers') from None
    if limit<1 or offset<0:raise ValueError('Page limit and offset must be positive')
    with database() as db:
        init_tables(db)
        db.row_factory=__import__('sqlite3').Row
        rows=db.execute('SELECT * FROM trade_events'+where+' ORDER BY occurred_at DESC,id LIMIT ? OFFSET ?',values+[min(limit,1_000_000),offset]).fetchall()
        total=db.execute('SELECT count(*) FROM trade_events'+where,values).fetchone()[0]
        counts={row[0]:row[1] for row in db.execute('SELECT mode,count(*) FROM trade_events GROUP BY mode')}
        symbols=[row[0] for row in db.execute('SELECT DISTINCT symbol FROM trade_events ORDER BY symbol')]
    return {'events':[{k:r[k] for k in r.keys() if k!='raw'} for r in rows], 'total':total,'offset':offset,'counts':counts,'symbols':symbols}


def _trade_side(side):
    if side not in (None,'','long','short'):raise ValueError('Unknown trade side')
    return side or None


def _round_trips(events):
    """Fold fills per mode+symbol into trades: entries accumulate until a close flattens the position."""
    trips=[];live={}
    for e in sorted(events,key=lambda e:(e['occurred_at'],e['id'])):
        key=(e['mode'],e['symbol']);trip=live.get(key)
        if trip is None:
            trip=live[key]={'mode':e['mode'],'symbol':e['symbol'],'opened_at':e['occurred_at'],'closed_at':None,'side':None,
                            'entry_qty':0.0,'entry_value':0.0,'exit_qty':0.0,'exit_value':0.0,'reported_pnl':None,'fills':0,'ids':[]}
            trips.append(trip)
        trip['fills']+=1;trip['ids'].append(e['id'])
        if e['event'] in ('close','partial_close'):
            trip['exit_qty']+=e['quantity'];trip['exit_value']+=e['quantity']*e['price']
            if e['reported_pnl'] is not None:trip['reported_pnl']=(trip['reported_pnl'] or 0)+e['reported_pnl']
            if e['event']=='close' or trip['exit_qty']>=trip['entry_qty']*(1-1e-9):
                trip['closed_at']=e['occurred_at'];del live[key]
        else:
            if trip['side'] is None:trip['side']='long' if e['side'].lower() in ('buy','long') else 'short'
            trip['entry_qty']+=e['quantity'];trip['entry_value']+=e['quantity']*e['price']
    for t in trips:
        entry_qty,exit_qty=t.pop('entry_qty'),t.pop('exit_qty')
        t['entry_price']=t.pop('entry_value')/entry_qty if entry_qty else None
        t['exit_price']=t.pop('exit_value')/exit_qty if exit_qty else None
    return trips


def list_trades(mode=None,symbol=None,month=None,limit=50,offset=0,side=None):
    """One row per round trip; month matches the trade's open or close month (UTC)."""
    side=_trade_side(side)
    data=list_events(mode,symbol,None,limit=1_000_000)
    if month:list_events(month=month,limit=1)  # validates the month format
    try:limit,offset=int(limit),int(offset)
    except (ValueError,TypeError):raise ValueError('Page limit and offset must be whole numbers') from None
    if limit<1 or offset<0:raise ValueError('Page limit and offset must be positive')
    trips=[t for t in _round_trips(data['events']) if (not month or month in (t['opened_at'][:7],(t['closed_at'] or '')[:7])) and (not side or t['side']==side)]
    trips.sort(key=lambda t:t['closed_at'] or t['opened_at'],reverse=True)
    return {'trades':[{k:v for k,v in t.items() if k!='ids'} for t in trips[offset:offset+limit]],'total':len(trips),'offset':offset,'counts':data['counts'],'symbols':data['symbols']}


def export_csv(mode=None,symbol=None,month=None,side=None):
    side=_trade_side(side)
    rows=list_events(mode,symbol,month,limit=20000)
    if rows['total']>20000:raise ValueError('Narrow the filters to at most 20,000 events for export')
    if side:
        # Buy/sell events only get a long/short side from the round trip they belong to.
        kept={i for t in _round_trips(list_events(mode,symbol,limit=1_000_000)['events']) if t['side']==side for i in t['ids']}
        rows['events']=[e for e in rows['events'] if e['id'] in kept]
    fields=['id','mode','symbol','event','side','occurred_at','quantity','price','reported_pnl','reported_r','fee','fee_asset','source','imported_at']
    output=io.StringIO();writer=csv.DictWriter(output,fields,extrasaction='ignore');writer.writeheader()
    for row in rows['events']:
        # Spreadsheet-safe text cells without modifying archived raw values.
        writer.writerow({k:("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v) for k,v in row.items()})
    return output.getvalue()
