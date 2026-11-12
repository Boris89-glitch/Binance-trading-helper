"""Does the price-stripped news line lead BTC? Pre-registered primary test: Spearman correlation of
the events line with the NEXT 1-day return, judged against a circular-shift placebo, required to
hold in both halves of the year and to beat the line's correlation with the PAST day's return."""
from datetime import datetime, timezone
import json
import sys

import numpy as np
from scipy.stats import rankdata, spearmanr

from .common import DATA, STUDY_START, STUDY_END, read_jsonl
from .line import BAR, build, prices

HORIZONS = {'1h': 4, '4h': 16, '1d': 96, '3d': 288}
PRIMARY = '1d'
VARIANTS = {'events': 'Events only (the signal)', 'major_events': 'Market-moving events only',
            'all': 'All relevant headlines incl. market reactions', 'price': 'Price reports only (echo control)'}
SHIFTS = 1000
RNG = np.random.default_rng(11)


def zrank(x):
    r = rankdata(x); return (r - r.mean()) / (r.std() or 1)


def corr_with_placebo(signal, target, shifts=SHIFTS):
    ok = np.isfinite(target)
    zt = zrank(target[ok]); n = len(signal)
    real = float(np.mean(zrank(signal[ok]) * zt))
    lo = 7 * 96
    ks = RNG.integers(lo, n - lo, size=shifts)
    null = np.array([np.mean(zrank(np.roll(signal, k)[ok]) * zt) for k in ks])
    p = (1 + np.sum(np.abs(null) >= abs(real))) / (1 + shifts)
    return {'rho': round(real, 4), 'p_placebo': round(float(p), 4), 'null_95': round(float(np.quantile(np.abs(null), .95)), 4), 'n': int(ok.sum())}


def returns(o):
    fwd = {}
    for name, h in HORIZONS.items():
        f = np.full(len(o), np.nan); f[:-h] = o[h:] / o[:-h] - 1; fwd[name] = f
    back = np.full(len(o), np.nan); back[96:] = o[96:] / o[:-96] - 1
    return fwd, back


def crossings(line, level, cooldown=96):
    up = level > 0
    hits = np.where((line[1:] > level) & (line[:-1] <= level))[0] + 1 if up else np.where((line[1:] < level) & (line[:-1] >= level))[0] + 1
    kept, last = [], -10**9
    for i in hits:
        if i - last >= cooldown: kept.append(i); last = i
    return np.array(kept, dtype=int)


def event_study(line, fwd):
    out = {}
    for level in (50, -50):
        idx = crossings(line, level); row = {'events': len(idx)}
        for name in ('1d', '3d'):
            f = fwd[name]; valid = idx[np.isfinite(f[idx])]
            if len(valid) == 0: continue
            mean = float(np.nanmean(f[valid]))
            pool = np.where(np.isfinite(f))[0]
            null = np.array([np.mean(f[RNG.choice(pool, len(valid))]) for _ in range(2000)])
            row[name] = {'mean_return_pct': round(mean * 100, 3), 'random_entry_mean_pct': round(float(null.mean()) * 100, 3),
                         'percentile_vs_random': round(float((null < mean).mean()) * 100, 1), 'hit_rate_up_pct': round(float((f[valid] > 0).mean()) * 100, 1)}
        out[f'cross {"above +" if level > 0 else "below "}{level}'] = row
    return out


def zone_edge(line, fwd, lo, hi):
    """Mean signed forward return over bars in the long (> hi) or short (< lo) zone; 0 if none."""
    pos = np.where(line > hi, 1.0, np.where(line < lo, -1.0, 0.0))
    ok = (pos != 0) & np.isfinite(fwd)
    return (float(np.mean(pos[ok] * fwd[ok])), int(ok.sum())) if ok.any() else (0.0, 0)


def threshold_sweep(t, line, fwd, study, half, horizon='1d', shifts=400):
    """Thresholds as percentiles of the line, chosen on the first half only and then tested
    untouched on the second half; placebo = circular time-shifts of the second-half line."""
    idx = np.arange(len(t)); h1 = study & (idx < half); h2 = study & (idx >= half); f = fwd[horizon]
    rows = []
    for q in (2, 5, 10, 20, 30):
        lo, hi = np.percentile(line[h1], [q, 100 - q])
        if hi <= lo: continue
        e1, n1 = zone_edge(line[h1], f[h1], lo, hi)
        rows.append({'pct': q, 'short_below': round(float(lo), 1), 'long_above': round(float(hi), 1), 'h1_edge_pct': round(e1 * 100, 3), 'h1_bars': n1})
    best = max(rows, key=lambda r: r['h1_edge_pct'])
    lo, hi = best['short_below'], best['long_above']
    s2, f2 = line[h2], f[h2]
    e2, n2 = zone_edge(s2, f2, lo, hi)
    null = np.array([zone_edge(np.roll(s2, k), f2, lo, hi)[0] for k in RNG.integers(7 * 96, len(s2) - 7 * 96, size=shifts)])
    return {'horizon': horizon, 'candidates_h1': rows, 'chosen_on_h1': best,
            'h2_edge_pct': round(e2 * 100, 3), 'h2_bars': n2, 'h2_long_bars': int(np.sum(s2 > hi)), 'h2_short_bars': int(np.sum(s2 < lo)),
            'h2_placebo_p': round(float((1 + np.sum(null >= e2)) / (1 + shifts)), 4)}


def sweep(variants=('events', 'all', 'price')):
    t, o = prices()
    start = datetime(STUDY_START.year, STUDY_START.month, STUDY_START.day, tzinfo=timezone.utc).timestamp()
    end = datetime(STUDY_END.year, STUDY_END.month, STUDY_END.day, tzinfo=timezone.utc).timestamp() + 86400
    fwd, _ = returns(o); study = (t >= start) & (t < end); half = np.flatnonzero(study)[study.sum() // 2]
    out = {}
    for key in variants:
        line, _ = build(t, key)
        out[key] = {h: threshold_sweep(t, line, fwd, study, half, h) for h in ('4h', '1d', '3d')}
        for h, r in out[key].items():
            c = r['chosen_on_h1']
            print(f'{key} {h}: H1 pick {c["pct"]}% (short<{c["short_below"]}, long>{c["long_above"]}, H1 edge {c["h1_edge_pct"]:+.3f}%) '
                  f'-> H2 edge {r["h2_edge_pct"]:+.3f}% over {r["h2_bars"]} bars (long {r["h2_long_bars"]}, short {r["h2_short_bars"]}), placebo p {r["h2_placebo_p"]}', flush=True)
    (DATA / 'threshold_sweep.json').write_text(json.dumps(out, indent=1), encoding='utf-8')
    return out


def hindsight():
    plain = {r['id']: r for r in read_jsonl(DATA / 'scores.jsonl')}
    masked = [r for r in read_jsonl(DATA / 'scores_entity_masked.jsonl') if r['id'] in plain]
    if len(masked) < 50: return None
    a = np.array([plain[r['id']]['rating'] for r in masked]); b = np.array([r['rating'] for r in masked])
    return {'n': len(masked), 'spearman': round(float(spearmanr(a, b).statistic), 3), 'exact_agreement_pct': round(float((a == b).mean()) * 100, 1),
            'mean_abs_diff': round(float(np.abs(a - b).mean()), 3), 'mean_shift_masked_minus_named': round(float((b - a).mean()), 3),
            'category_agreement_pct': round(float(np.mean([plain[r['id']]['category'] == r['category'] for r in masked])) * 100, 1)}


def run():
    t, o = prices()
    start = datetime(STUDY_START.year, STUDY_START.month, STUDY_START.day, tzinfo=timezone.utc).timestamp()
    end = datetime(STUDY_END.year, STUDY_END.month, STUDY_END.day, tzinfo=timezone.utc).timestamp() + 86400
    gaps = int(np.sum(np.diff(t) != BAR))
    fwd, back = returns(o)
    study = (t >= start) & (t < end)
    half = np.flatnonzero(study)[study.sum() // 2]
    report = {'generated_at': datetime.now(timezone.utc).isoformat(), 'bars': int(study.sum()), 'price_gaps': gaps, 'primary': PRIMARY, 'variants': {}}
    for key, title in VARIANTS.items():
        line, count = build(t, key)
        s = line[study]
        v = {'title': title, 'coverage_pct': round(float((count[study] > 0).mean()) * 100, 1), 'mean': round(float(s.mean()), 2), 'std': round(float(s.std()), 2),
             'pct_above_50': round(float((s > 50).mean()) * 100, 2), 'pct_below_-50': round(float((s < -50).mean()) * 100, 2),
             'forward': {h: corr_with_placebo(s, fwd[h][study]) for h in HORIZONS},
             'echo_past_1d': corr_with_placebo(s, back[study])}
        for name, mask in (('first_half', study & (np.arange(len(t)) < half)), ('second_half', study & (np.arange(len(t)) >= half))):
            v[name] = {'forward_1d': corr_with_placebo(line[mask], fwd[PRIMARY][mask], 400), 'echo_past_1d': corr_with_placebo(line[mask], back[mask], 400)}
        if key == 'events':
            v['event_study'] = event_study(s, {h: fwd[h][study] for h in fwd})
            q = np.quantile(s, [.2, .4, .6, .8]); bucket = np.searchsorted(q, s)
            v['quintiles_fwd_1d_pct'] = [round(float(np.nanmean(fwd['1d'][study][bucket == i])) * 100, 3) for i in range(5)]
            months = {}
            for ts_, c in zip(t[study][::96], count[study][::96]): months.setdefault(datetime.fromtimestamp(ts_, timezone.utc).strftime('%Y-%m'), []).append(int(c))
            v['median_headlines_in_window_by_month'] = {m: int(np.median(c)) for m, c in months.items()}
            np.save(DATA / 'line_events.npy', np.vstack([t[study], s, o[study]]))
        report['variants'][key] = v
        print(f'{key}: fwd1d rho {v["forward"]["1d"]["rho"]} p {v["forward"]["1d"]["p_placebo"]} · echo rho {v["echo_past_1d"]["rho"]} p {v["echo_past_1d"]["p_placebo"]}', flush=True)
    report['hindsight'] = hindsight()
    report['verdict'] = verdict(report)
    (DATA / 'report.json').write_text(json.dumps(report, indent=1), encoding='utf-8')
    (DATA / 'report.md').write_text(markdown(report), encoding='utf-8')
    print(report['verdict'], flush=True)
    return report


def verdict(r):
    e = r['variants']['events']; f = e['forward'][PRIMARY]; echo = e['echo_past_1d']
    halves = [e[h]['forward_1d'] for h in ('first_half', 'second_half')]
    checks = {'significant_vs_placebo': f['p_placebo'] < 0.05,
              'same_sign_both_halves': all(np.sign(h['rho']) == np.sign(f['rho']) and h['p_placebo'] < 0.10 for h in halves),
              'leads_more_than_echoes': abs(f['rho']) > abs(echo['rho']),
              'positive_direction': f['rho'] > 0}
    passed = all(checks.values())
    return {'pass': passed, 'checks': checks, 'summary': ('PASS: worth a strategy backtest' if passed else 'FAIL: no reliable lead over price') +
            f' (events line vs next-1d return rho {f["rho"]:+.3f}, placebo p {f["p_placebo"]}; echo rho {echo["rho"]:+.3f})'}


def markdown(r):
    lines = [f'# News sentiment vs BTC — validation study', '', f'Generated {r["generated_at"][:16]} UTC · {r["bars"]} 15m bars {STUDY_START} → {STUDY_END} · price gaps {r["price_gaps"]}', '',
             f'**Verdict: {r["verdict"]["summary"]}**', '', 'Checks: ' + ', '.join(f'{k} {"✓" if v else "✗"}' for k, v in r['verdict']['checks'].items()), '',
             '| Line | coverage | 1h ρ (p) | 4h ρ (p) | **1d ρ (p)** | 3d ρ (p) | echo: past 1d ρ (p) | 1d H1 / H2 ρ |', '|---|---|---|---|---|---|---|---|']
    for key, v in r['variants'].items():
        cell = lambda c: f'{c["rho"]:+.3f} ({c["p_placebo"]})'
        lines.append(f'| {v["title"]} | {v["coverage_pct"]}% | ' + ' | '.join(cell(v['forward'][h]) for h in HORIZONS) +
                     f' | {cell(v["echo_past_1d"])} | {v["first_half"]["forward_1d"]["rho"]:+.3f} / {v["second_half"]["forward_1d"]["rho"]:+.3f} |')
    e = r['variants']['events']
    lines += ['', 'ρ = Spearman rank correlation; p = share of 1,000 circular time-shifts of the same line with |ρ| at least as large (placebo).', '',
              f'Events line: mean {e["mean"]}, sd {e["std"]}, above +50 {e["pct_above_50"]}% of bars, below −50 {e["pct_below_-50"]}%.', '',
              'Next-1d return by events-line quintile (low → high), %: ' + ', '.join(f'{x:+.3f}' for x in e['quintiles_fwd_1d_pct']), '', '**Threshold crossings (1-day cooldown)**', '']
    for name, row in e['event_study'].items():
        lines.append(f'- {name}: {row["events"]} events' + ''.join(f'; {h} mean {row[h]["mean_return_pct"]:+.2f}% vs random {row[h]["random_entry_mean_pct"]:+.2f}% (pctile {row[h]["percentile_vs_random"]}, up {row[h]["hit_rate_up_pct"]}%)' for h in ('1d', '3d') if h in row))
    h = r['hindsight']
    lines += ['', '**Hindsight check** (same headlines, well-known names replaced by generic roles): ' +
              (f'n {h["n"]}, rating Spearman {h["spearman"]}, exact agreement {h["exact_agreement_pct"]}%, mean |diff| {h["mean_abs_diff"]}, masked−named {h["mean_shift_masked_minus_named"]:+}, category agreement {h["category_agreement_pct"]}%' if h else 'not run'),
              '', 'Median scored event headlines in the 7-day window, by month: ' + ', '.join(f'{m} {c}' for m, c in e['median_headlines_in_window_by_month'].items())]
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    sweep() if 'sweep' in sys.argv else run()
