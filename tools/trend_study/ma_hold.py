"""BTC 4h moving-average hold filter vs buy & hold (standalone; not the crypto bot's OCO engine).

Rule: be long while the last CLOSED 4h candle is above its N-bar simple moving average, else in
cash. A minimum hold keeps a new position for at least H bars before a close below the average
can exit it. Signals act at the next bar's open. Fee per side on every entry/exit."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np

DATA = Path(__file__).resolve().parents[2] / '.macro_helper_local' / 'trend_study'
BARS_PER_DAY = 6
PERIODS = [('full', '2018-01-01', '2100'), ('2018-21', '2018-01-01', '2022-01-01'), ('2022-26', '2022-01-01', '2100')]
VARIANTS = [  # (label, MA bars, min hold bars)
    ('4h MA50, no min hold', 50, 0), ('4h MA50, hold ≥2d', 50, 12), ('4h MA50, hold ≥3d', 50, 18), ('4h MA50, hold ≥5d', 50, 30),
    ('4h MA300 (≈50-day)', 300, 0), ('4h MA600 (≈100-day)', 600, 0)]


def candles(symbol='BTCUSDT'):
    DATA.mkdir(parents=True, exist_ok=True); path = DATA / f'{symbol}_4h.json'
    if path.exists(): return json.loads(path.read_text())
    rows, cursor = [], int(datetime(2017, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)
    now = datetime.now(timezone.utc).timestamp() * 1000
    while True:
        batch = json.loads(urlopen(Request('https://api.binance.com/api/v3/klines?' + urlencode(
            {'symbol': symbol, 'interval': '4h', 'startTime': cursor, 'limit': 1000}), headers={'User-Agent': 'Mozilla/5.0'}), timeout=30).read())
        rows += [[r[0] // 1000, float(r[1]), float(r[4])] for r in batch if r[6] < now]   # completed candles only
        if len(batch) < 1000: break
        cursor = batch[-1][0] + 4 * 3600_000
    data = {'t': [r[0] for r in rows], 'open': [r[1] for r in rows], 'close': [r[2] for r in rows]}
    path.write_text(json.dumps(data)); return data


def positions(close, n, hold):
    sma = np.full(len(close), np.nan); c = np.cumsum(np.insert(close, 0, 0)); sma[n - 1:] = (c[n:] - c[:-n]) / n
    above = close > sma
    pos = np.zeros(len(close)); held = 0
    for i in range(len(close)):          # pos[i]: position after bar i closes
        if pos[i - 1] if i else 0:
            held += 1
            pos[i] = 1 if (above[i] or held < hold) else 0
        else:
            held = 0; pos[i] = 1 if above[i] else 0
    pos[np.isnan(sma)] = 0
    return pos


def stats(pos, o, fee):
    """pos[i] decided at close of bar i, held from open i+1 to open i+2."""
    ret = o[2:] / o[1:-1] - 1; held = pos[:-2]
    change = np.abs(np.diff(np.concatenate([[0], held])))
    equity = np.cumprod(1 + held * ret - fee * change)
    years = len(ret) / (BARS_PER_DAY * 365)
    trades = []; start = None
    for i, h in enumerate(held):
        if h and start is None: start = i
        if not h and start is not None: trades.append(np.prod(1 + ret[start:i]) - 1 - 2 * fee); start = None
    trades = np.array(trades)
    return {'cagr_pct': round((equity[-1] ** (1 / years) - 1) * 100, 1), 'total_pct': round((equity[-1] - 1) * 100),
            'max_dd_pct': round(float((equity / np.maximum.accumulate(equity) - 1).min()) * 100, 1),
            'in_market_pct': round(float(held.mean()) * 100), 'trades': len(trades),
            'win_rate_pct': round(float((trades > 0).mean()) * 100) if len(trades) else None,
            'avg_hold_days': round(float(held.sum()) / max(len(trades), 1) / BARS_PER_DAY, 1)}


def run(fee=0.001):
    d = candles(); t = np.array(d['t']); o = np.array(d['open']); c = np.array(d['close'])
    stamp = np.array([datetime.fromtimestamp(x, timezone.utc).strftime('%Y-%m-%d') for x in t])
    out = {}
    for name, a, b in PERIODS:
        m = (stamp >= a) & (stamp < b); out[name] = {'buy & hold': stats(np.ones(m.sum()), o[m], fee)}
        for label, n, hold in VARIANTS:
            pos = positions(c, n, hold)    # computed on full history so the MA is warm at period start
            out[name][label] = stats(pos[m], o[m], fee)
    (DATA / f'ma_hold_fee{fee}.json').write_text(json.dumps(out, indent=1), encoding='utf-8')
    return out


if __name__ == '__main__':
    fee = float(sys.argv[1]) if len(sys.argv) > 1 else 0.001
    for period, rows in run(fee).items():
        print(f'\n== {period} · fee {fee * 100:.2f}%/side ==')
        print(f'{"":24}{"CAGR":>7}{"total":>9}{"maxDD":>8}{"in mkt":>8}{"trades":>8}{"win%":>6}{"avg d":>7}')
        for label, s in rows.items():
            print(f'{label:<24}{s["cagr_pct"]:>6}%{s["total_pct"]:>8}%{s["max_dd_pct"]:>7}%{s["in_market_pct"]:>7}%{s["trades"]:>8}{str(s["win_rate_pct"]):>6}{s["avg_hold_days"]:>7}')
