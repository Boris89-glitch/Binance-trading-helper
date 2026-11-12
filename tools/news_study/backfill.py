"""Day-by-day Google News RSS backfill: one search per query per day, so the 100-result cap
cannot hide days. Google's archive thins out for older dates; the line weights tone and timing,
not volume, so sparse periods are accepted. Each (query, day) response is cached forever and
reruns resume where they stopped."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import random
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from .common import DATA, STUDY_START, STUDY_END, WARMUP_DAYS, PRICE_REPORT, days
from tools.macro_helper.recent_news import PUBLISHERS, SPAM, norm

CACHE = DATA / 'gnews'
# key: (tier hint, query). Price-commentary searches ("bitcoin price", market wraps) are deliberately absent.
# Keep each query under ~150 characters: Google silently ignores after:/before: on longer ones.
QUERIES = {
    'major_events': ('major', '(bitcoin OR crypto) (hack OR exploit OR heist OR breach OR insolvency OR bankruptcy OR lawsuit OR sanctions)'),
    'major_policy': ('major', '(bitcoin OR crypto) (election OR midterms OR Congress OR "White House" OR SEC OR CFTC OR regulation OR ETF OR Fed OR tariff)'),
    'market': ('market', '("crypto industry" OR "crypto firms" OR "crypto exchange" OR stablecoin OR tokenization OR "crypto adoption" OR "crypto bank")'),
    'coin': ('coin', '"bitcoin" (crypto OR cryptocurrency OR token OR coin OR blockchain)'),
}
CRYPTO_PRESS = ('cryptoslate.com', 'cryptobriefing.com', 'bravenewcoin.com', 'protos.com', 'unchainedcrypto.com',
                'beincrypto.com', 'crypto.news', 'cryptonews.com', 'cryptopolitan.com', 'coinpedia.org', 'tokenpost.com')
DOMAINS = PUBLISHERS + CRYPTO_PRESS          # quality filter for every tier
PER_DAY_CAP = {'major': 24, 'market': 12, 'coin': 16}   # evenly time-sampled; binds only on busy recent days
PRICE_CONTROL_CAP = 6
PACE = 1.5
state = {'last': 0.0}


def get(url):
    for attempt in range(8):
        wait = PACE + random.random() - (time.time() - state['last'])
        if wait > 0: time.sleep(wait)
        state['last'] = time.time()
        try:
            with urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0 (news sentiment research)'}), timeout=30) as response:
                return response.read(6_000_000).decode('utf-8-sig')
        except (HTTPError, URLError, TimeoutError) as exc:
            pause = 60 * (attempt + 1) if getattr(exc, 'code', None) in (429, 503) else 10 * (attempt + 1)
            print(f'  {exc}; waiting {pause}s', flush=True); time.sleep(pause)
    raise RuntimeError('Google News kept failing; rerun later to resume')


def fetch_day(key, day):
    path = CACHE / key / f'{day}.json'
    if path.exists(): return json.loads(path.read_text(encoding='utf-8'))
    q = f'{QUERIES[key][1]} after:{day - timedelta(days=1)} before:{day + timedelta(days=1)}'
    url = 'https://news.google.com/rss/search?' + urlencode({'q': q, 'hl': 'en-US', 'gl': 'US', 'ceid': 'US:en'})
    items = []
    for node in ET.fromstring(get(url)).findall('./channel/item'):
        try: published = parsedate_to_datetime(node.findtext('pubDate')).astimezone(timezone.utc)
        except (ValueError, TypeError, AttributeError): continue
        source = node.find('source')
        items.append({'title': node.findtext('title', ''), 'url': node.findtext('link', ''), 'published_at': published.isoformat(),
                      'publisher': source.text if source is not None else '', 'publisher_url': source.get('url', '') if source is not None else ''})
    data = {'url': url, 'retrieved_at': datetime.now(timezone.utc).isoformat(), 'items': items}
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(data), encoding='utf-8')
    return data


def run():
    span = days(STUDY_START - timedelta(days=WARMUP_DAYS), STUDY_END)
    todo = [(k, d) for k in QUERIES for d in span if not (CACHE / k / f'{d}.json').exists()]
    print(f'backfill: {len(todo)} of {len(span) * len(QUERIES)} query-days to fetch', flush=True)
    for i, (key, day) in enumerate(todo, 1):
        fetch_day(key, day)
        if i % 50 == 0: print(f'  {i}/{len(todo)}', flush=True)
    build()


def keep(item, day):
    host = (urlparse(item['publisher_url']).hostname or '').lower()
    return (item['published_at'][:10] == str(day) and item['url'].startswith('https://') and item['title'].strip()
            and any(host == d or host.endswith('.' + d) for d in DOMAINS) and not SPAM.search(item['title']))


def sample(group, cap):
    return group if len(group) <= cap else [group[round(i * (len(group) - 1) / (cap - 1))] for i in range(cap)]


def build():
    """Filter, dedupe (heavier tiers first) and evenly time-sample each day into one table."""
    seen = set(); rows = []; stats = {}
    for key, (tier, _) in QUERIES.items():
        s = stats.setdefault(key, {'days': 0, 'returned': 0, 'quality': 0, 'price_regex': 0, 'sampled': 0})
        for path in sorted((CACHE / key).glob('*.json')):
            day = datetime.strptime(path.stem, '%Y-%m-%d').date()
            data = json.loads(path.read_text(encoding='utf-8'))
            s['days'] += 1; s['returned'] += len(data['items'])
            pool, price_pool = [], []
            for item in sorted(data['items'], key=lambda i: i['published_at']):
                if not keep(item, day): continue
                story = norm(item['title'])
                if story in seen: continue
                seen.add(story); s['quality'] += 1
                price = bool(PRICE_REPORT.search(item['title'])); s['price_regex'] += price
                (price_pool if price else pool).append(item)
            # Price reports are excluded from the signal; a small sample is kept as an echo
            # control (a line built from them should track PAST returns).
            for group, cap, is_price in ((pool, PER_DAY_CAP[tier], False), (price_pool, PRICE_CONTROL_CAP, True)):
                picked = sample(group, cap); s['sampled'] += len(picked)
                rows += [{'id': 'h' + hashlib.sha1(i['url'].encode()).hexdigest()[:10], 'query': key, 'tier_hint': tier,
                          'title': i['title'][:300], 'publisher': i['publisher'], 'published_at': i['published_at'],
                          'url': i['url'], 'price_regex': is_price} for i in picked]
    rows.sort(key=lambda r: r['published_at'])
    (DATA / 'headlines.json').write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
    (DATA / 'backfill_stats.json').write_text(json.dumps(stats, indent=1), encoding='utf-8')
    print('headlines:', len(rows), json.dumps(stats), flush=True)
    return rows


if __name__ == '__main__':
    (build if 'build' in sys.argv else run)()
