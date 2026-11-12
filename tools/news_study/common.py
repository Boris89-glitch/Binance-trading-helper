"""Shared paths, study window and price-report filters."""
from datetime import date, timedelta
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / '.macro_helper_local' / 'news_study'
STUDY_START = date(2025, 11, 5)          # first evaluated day
STUDY_END = date(2026, 11, 4)            # last completed day
WARMUP_DAYS = 7                          # headlines before STUDY_START feed the first bars
SYMBOL = 'BTCUSDT'

# Hard exclusion of headlines that plainly report price action, levels, predictions or flows.
PRICE_REPORT = re.compile(
    r"price (prediction|analysis|forecast|outlook|today|target|action|watch)|"
    r"\$\s?\d{1,3}(,\d{3})+(?!\s?(billion|million|bn|m)\b)|\$\s?\d+(\.\d+)?\s?k\b|\b\d+(\.\d+)?\s?%|"
    r"all-time high|record high|\bATH\b|liquidat|technical analysis|support level|resistance level|\bchart\b|"
    r"bull run|bear market|sell-?off|\brout\b|\boutflows?\b|\binflows?\b|"
    # Ambiguous verbs (hits, drops, tops, breaks, gains) are left to the AI classifier.
    r"\b(bitcoin|btc|crypto|cryptos|ether|ethereum|altcoins?|markets?|prices?|stocks?)\b\W+(\w+\W+)?"
    r"(tumbles?|plunges?|surges?|soars?|rall(y|ies)|slides?|slumps?|sinks?|jumps?|climbs?|rebounds?|bounces?|dips?|"
    r"falls?|rises?|crash(es)?|spikes?|retreats?|recovers?|steadies|slips?)\b",
    re.I)


def days(start, end):
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def read_jsonl(path):
    if not path.exists(): return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def append_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        for row in rows: handle.write(json.dumps(row, ensure_ascii=False) + '\n')
