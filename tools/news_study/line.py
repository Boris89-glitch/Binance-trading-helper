"""BTC 15m prices and the deterministic 15m sentiment line built from cached headline scores.

Line at bar open T uses only headlines first seen strictly before T, within 7 days:
    S(T) = 50 × Σ w·r·f / (Σ w·f + 1),  w = tier weight (3/2/1),  f = 0.5^(age / 3 days)
the same weighting as the live pulse, applied per headline instead of per narrative."""
from datetime import datetime, timedelta, timezone
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np

from .common import DATA, STUDY_START, STUDY_END, SYMBOL, read_jsonl
from tools.macro_helper.recent_news import HALF_LIFE_DAYS, NEUTRAL_ANCHOR, TIERS

BAR = 15 * 60
WINDOW = 7 * 86400
WEIGHTS = {k: v['weight'] for k, v in TIERS.items()}


def prices():
    """Binance USDT-M 15m candles: open time (s) and open price, cached."""
    path = DATA / f'{SYMBOL}_15m.json'
    start = int(datetime.combine(STUDY_START - timedelta(days=8), datetime.min.time(), timezone.utc).timestamp())
    end = int(datetime.combine(STUDY_END + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp())
    if path.exists():
        data = json.loads(path.read_text(encoding='utf-8'))
        if data['start'] == start and data['end'] == end: return np.array(data['t']), np.array(data['open'])
    t, o = [], []; cursor = start * 1000
    while cursor < end * 1000:
        query = urlencode({'symbol': SYMBOL, 'interval': '15m', 'startTime': cursor, 'endTime': end * 1000 - 1, 'limit': 1500})
        rows = json.loads(urlopen(Request('https://fapi.binance.com/fapi/v1/klines?' + query, headers={'User-Agent': 'Mozilla/5.0'}), timeout=30).read())
        if not rows: break
        for r in rows: t.append(r[0] // 1000); o.append(float(r[1]))
        cursor = rows[-1][0] + BAR * 1000
    path.write_text(json.dumps({'start': start, 'end': end, 't': t, 'open': o}), encoding='utf-8')
    return np.array(t), np.array(o)


def scored(variant):
    """Headlines joined with scores. variant: events (signal), major_events, all (incl. reactions), price (echo control)."""
    heads = {h['id']: h for h in json.loads((DATA / 'headlines.json').read_text(encoding='utf-8'))}
    out = []
    for s in read_jsonl(DATA / 'scores.jsonl'):
        h = heads.get(s['id'])
        if not h: continue
        event = s['category'] == 'event' and not h.get('price_regex')
        use = {'events': event, 'major_events': event and s['tier'] == 'major',
               'all': s['category'] != 'irrelevant', 'price': h.get('price_regex') or s['category'] == 'market_reaction'}[variant]
        if use:
            out.append((datetime.fromisoformat(h['published_at']).timestamp(), WEIGHTS[s['tier']], s['rating']))
    out.sort()
    return np.array(out) if out else np.zeros((0, 3))


def build(times, variant='events'):
    rows = scored(variant)
    ts, w, r = (rows[:, 0], rows[:, 1], rows[:, 2]) if len(rows) else (np.array([]),) * 3
    hi = np.searchsorted(ts, times, side='left')          # strictly before bar open
    lo = np.searchsorted(ts, times - WINDOW, side='left')
    line = np.zeros(len(times)); count = hi - lo
    for i, T in enumerate(times):
        a, b = lo[i], hi[i]
        if b > a:
            f = 0.5 ** ((T - ts[a:b]) / (HALF_LIFE_DAYS * 86400))
            wf = w[a:b] * f
            line[i] = 50 * float(wf @ r[a:b]) / (float(wf.sum()) + NEUTRAL_ANCHOR)
    return line, count
