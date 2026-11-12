"""Buy fear or buy FOMO? Tests the alternative.me Crypto Fear & Greed Index (daily, since 2018-02)
against BTC. Fixed, standard zones (extreme fear < 25, extreme greed > 75), no tuning.

Timing: the value stamped day D is treated as known only at the END of day D, so every entry is
at the next day's open (conservative, no look-ahead). Placebo = circular time-shifts of the index.
Note the index is built partly from price momentum and volatility, so it is expected to echo past
price; the question is only whether it leads."""
from datetime import datetime, timezone
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
from scipy.stats import rankdata

from .common import DATA

RNG = np.random.default_rng(5)
ZONES = [('extreme fear', 0, 25), ('fear', 25, 45), ('neutral', 45, 55), ('greed', 55, 75), ('extreme greed', 75, 101)]
PERIODS = [('2018-02 → 2021-12', '2018-02-01', '2022-01-01'), ('2022-01 → 2026-11', '2022-01-01', '2026-12-01')]
FEE = 0.001   # per side, applied on every position change


def get(url):
    return json.loads(urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=30).read())


def load():
    path = DATA / 'fear_greed.json'
    if path.exists(): return json.loads(path.read_text(encoding='utf-8'))
    fng = {datetime.fromtimestamp(int(r['timestamp']), timezone.utc).date().isoformat(): int(r['value'])
           for r in get('https://api.alternative.me/fng/?limit=0&format=json')['data']}
    opens = {}; cursor = int(datetime(2018, 1, 25, tzinfo=timezone.utc).timestamp() * 1000)
    while True:
        rows = get('https://api.binance.com/api/v3/klines?' + urlencode({'symbol': 'BTCUSDT', 'interval': '1d', 'startTime': cursor, 'limit': 1000}))
        if not rows: break
        for r in rows: opens[datetime.fromtimestamp(r[0] / 1000, timezone.utc).date().isoformat()] = float(r[1])
        if len(rows) < 1000: break
        cursor = rows[-1][0] + 86400_000
    days = sorted(d for d in fng if d in opens)
    data = {'days': days, 'fng': [fng[d] for d in days], 'open': [opens[d] for d in days]}
    path.write_text(json.dumps(data), encoding='utf-8')
    return data


def zr(x):
    r = rankdata(x); return (r - r.mean()) / (r.std() or 1)


def placebo_corr(x, y, shifts=2000):
    ok = np.isfinite(y); zy = zr(y[ok]); real = float(np.mean(zr(x[ok]) * zy))
    null = np.array([np.mean(zr(np.roll(x, k)[ok]) * zy) for k in RNG.integers(60, len(x) - 60, size=shifts)])
    return round(real, 3), round(float((1 + np.sum(np.abs(null) >= abs(real))) / (1 + shifts)), 4)


def backtest(pos, entry_open):
    """pos[i] = position held from open i+1 to open i+2 (decided from index value of day i)."""
    ret = np.zeros(len(pos))
    ret[:-2] = entry_open[2:] / entry_open[1:-1] - 1
    held = pos[:-2]; r = held * ret[:-2] - FEE * np.abs(np.diff(np.concatenate([[0], held])))
    equity = np.cumprod(1 + r)
    years = len(r) / 365
    peak = np.maximum.accumulate(equity)
    return {'total_return_pct': round((equity[-1] - 1) * 100, 1), 'cagr_pct': round((equity[-1] ** (1 / years) - 1) * 100, 1),
            'max_drawdown_pct': round(float((equity / peak - 1).min()) * 100, 1), 'time_in_market_pct': round(float(np.mean(held != 0)) * 100, 1),
            'trades': int(np.sum(np.diff(np.concatenate([[0], held])) != 0))}


def run():
    data = load(); days = np.array(data['days']); x = np.array(data['fng'], float); o = np.array(data['open'])
    report = {'days': len(days), 'from': days[0], 'to': days[-1], 'periods': {}}
    for name, a, b in [('full', days[0], '2100')] + PERIODS:
        m = (days >= a) & (days < b); xs, os = x[m], o[m]
        fwd = {}
        for h in (1, 7, 30):
            f = np.full(len(os), np.nan); f[:-(h + 1)] = os[h + 1:] / os[1:-h] - 1; fwd[h] = f   # enter next open
        back7 = np.full(len(os), np.nan); back7[7:] = os[7:] / os[:-7] - 1
        zones = []
        for label, lo, hi in ZONES:
            z = (xs >= lo) & (xs < hi)
            zones.append({'zone': label, 'days': int(z.sum()), **{f'fwd_{h}d_mean_pct': round(float(np.nanmean(fwd[h][z])) * 100, 2) for h in (7, 30)},
                          'fwd_30d_up_pct': round(float(np.nanmean(fwd[30][z] > 0)) * 100, 1)})
        uncond = {f'fwd_{h}d_mean_pct': round(float(np.nanmean(fwd[h])) * 100, 2) for h in (7, 30)}
        strategies = {'buy & hold': np.ones(len(xs)), 'buy fear (long while < 25)': (xs < 25).astype(float),
                      'buy FOMO (long while > 75)': (xs > 75).astype(float), 'fade: long < 25, short > 75': np.where(xs < 25, 1.0, np.where(xs > 75, -1.0, 0.0)),
                      'trend: long > 55, flat otherwise': (xs > 55).astype(float)}
        report['periods'][name] = {'zones': zones, 'unconditional': uncond,
                                   'spearman_vs_forward': {f'{h}d': placebo_corr(xs, fwd[h]) for h in (1, 7, 30)},
                                   'spearman_vs_past_7d (echo)': placebo_corr(xs, back7),
                                   'strategies': {k: backtest(v, os) for k, v in strategies.items()}}
    (DATA / 'fear_greed_report.json').write_text(json.dumps(report, indent=1), encoding='utf-8')
    return report


if __name__ == '__main__':
    r = run()
    print(f"{r['days']} days {r['from']} -> {r['to']}")
    for name, p in r['periods'].items():
        print(f'\n== {name} ==  unconditional {p["unconditional"]}')
        print('  rho vs forward:', p['spearman_vs_forward'], '| echo past 7d:', p['spearman_vs_past_7d (echo)'])
        for z in p['zones']: print(f"  {z['zone']:<14}{z['days']:>5} days  fwd7 {z['fwd_7d_mean_pct']:+6.2f}%  fwd30 {z['fwd_30d_mean_pct']:+6.2f}%  30d up {z['fwd_30d_up_pct']}%")
        for k, s in p['strategies'].items(): print(f'  {k:<34} {s}')
