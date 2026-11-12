"""Recent (~7 day) tiered headline pulse for crypto reports: market-moving events,
broad crypto-market news and coin-specific news, with transparent time decay.

The model groups headlines into narratives and rates their tone, momentum and
next-few-days outlook; the server alone turns those ratings into scores."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime, timedelta, timezone
from email.utils import parsedate_to_datetime
import re
from urllib.parse import urlencode, urlparse
import xml.etree.ElementTree as ET

WINDOW_DAYS = 7
HALF_LIFE_DAYS = 3.0
NEUTRAL_ANCHOR = 1.0          # pseudo-weight at 0 that dampens thin coverage
MAX_NARRATIVES = 8
MAX_MAJOR = 3
TIERS = {
    'major': {'label': 'Market-moving events', 'weight': 3,
              # Google News silently drops after:/before: on long queries (~>165 chars), so split.
              'queries': ('(bitcoin OR crypto) (hack OR exploit OR heist OR breach OR insolvency OR bankruptcy OR lawsuit OR sanctions)',
                          '(bitcoin OR crypto) (election OR midterms OR Congress OR "White House" OR SEC OR CFTC OR regulation OR ETF OR Fed OR tariff)'),
              'per_day': 4, 'chunk_days': 1},
    'market': {'label': 'Broad crypto market', 'weight': 2,
               'queries': ('("crypto market" OR "crypto markets" OR "bitcoin price" OR "crypto prices" OR altcoins OR "crypto sentiment" OR "crypto investors" OR "ETF flows")',),
               'per_day': 3, 'chunk_days': 1},
    'coin': {'label': 'Coin-specific', 'weight': 1, 'per_day': 2, 'chunk_days': 4},
}
PERSISTENCE = {'building': 1.0, 'steady': 0.7, 'fading': 0.4}
# Market-wide tiers use established newsrooms and crypto trade press; SEO price-prediction
# sites dominate unfiltered results. Coin feeds stay unfiltered (small coins get little press).
PUBLISHERS = ('reuters.com', 'bloomberg.com', 'cnbc.com', 'wsj.com', 'ft.com', 'apnews.com', 'axios.com',
              'nytimes.com', 'washingtonpost.com', 'theguardian.com', 'bbc.com', 'bbc.co.uk', 'cnn.com', 'nbcnews.com',
              'npr.org', 'pbs.org', 'politico.com', 'forbes.com', 'fortune.com', 'barrons.com', 'marketwatch.com',
              'investors.com', 'finance.yahoo.com', 'businessinsider.com', 'economist.com', 'scmp.com', 'nikkei.com',
              'coindesk.com', 'theblock.co', 'decrypt.co', 'cointelegraph.com', 'blockworks.co', 'dlnews.com',
              'bitcoinmagazine.com', 'thedefiant.io', 'trmlabs.com', 'chainalysis.com', 'elliptic.co')
SPAM = re.compile(r'price prediction|price forecast \d{4}|could reach \$|will .{0,20} hit \$|to buy now|best crypto', re.I)
RATING = (-2, -1, 0, 1, 2)


def window(cutoff, today):
    """Seven days ending at the data cutoff; a live report also takes today's headlines so far."""
    start = cutoff - timedelta(days=WINDOW_DAYS - 1)
    live = cutoff >= today - timedelta(days=1)
    return start, (today if live else cutoff), live


def reference_time(last_day, live, now):
    return now if live else datetime.combine(last_day, dtime(23, 59, 59), timezone.utc)


def freshness(age_days):
    return round(0.5 ** (max(age_days, 0.0) / HALF_LIFE_DAYS), 3)


def label(score):
    if score is None: return 'insufficient'
    return 'very_bearish' if score <= -60 else 'bearish' if score <= -20 else 'very_bullish' if score >= 60 else 'bullish' if score >= 20 else 'neutral'


def norm(title):
    return re.sub(r'[^a-z0-9]+', ' ', title.lower().rsplit(' - ', 1)[0]).strip()


def chunks(start, last_day, size):
    day = start
    while day <= last_day:
        yield day, min(day + timedelta(days=size - 1), last_day); day += timedelta(days=size)


def parse(xml, tier, first, last, now):
    items = []
    for node in ET.fromstring(xml).findall('./channel/item'):
        try: published = parsedate_to_datetime(node.findtext('pubDate')).astimezone(timezone.utc)
        except (ValueError, TypeError, AttributeError): continue
        if not first <= published.date() <= last or published > now: continue
        link = node.findtext('link', ''); source = node.find('source'); title = node.findtext('title', '')
        publisher_url = source.get('url', '') if source is not None else ''
        host = (urlparse(publisher_url).hostname or '').lower()
        if not link.startswith('https://') or not title.strip() or SPAM.search(title): continue
        if tier != 'coin' and not any(host == d or host.endswith('.' + d) for d in PUBLISHERS): continue
        items.append({'title': title[:300], 'url': link, 'published_at': published.isoformat(), 'rank': len(items),
                      'publisher': source.text if source is not None else 'Unknown publisher', 'publisher_url': publisher_url})
    return items


def fetch_tier(tier, queries, start, last_day, live, refresh=False, key=None):
    """One search per query per day (coins: per half-window) so the 100-result cap cannot hide
    older days. Items keep the search engine's relevance rank within each search."""
    from .server import cached, get_url
    def fetch():
        items = []; now = datetime.now(timezone.utc)
        for query in queries:
            for first, last in chunks(start, last_day, TIERS[tier]['chunk_days']):
                q = f'{query} after:{first - timedelta(days=1)} before:{last + timedelta(days=1)}'
                url = 'https://news.google.com/rss/search?' + urlencode({'q': q, 'hl': 'en-US', 'gl': 'US', 'ceid': 'US:en'})
                items += parse(get_url(url), tier, first, last, now)
        return {'items': items}
    suffix = 'live' if live else 'closed'
    return {'tier': tier, 'label': TIERS[tier]['label'], 'queries': list(queries),
            **cached(f'recent_v2_{key or tier}_{start}_{last_day}_{suffix}', fetch, refresh)}


def collect(symbols, cutoff, ledger, refresh=False, today=None, now=None):
    """Fetch shared market tiers once plus one coin feed per symbol; returns the context block."""
    from .pairs import base_asset
    now = now or datetime.now(timezone.utc); today = today or now.date()
    start, last_day, live = window(cutoff, today)
    ref = reference_time(last_day, live, now)
    jobs = [('major', TIERS['major']['queries'], None), ('market', TIERS['market']['queries'], None)]
    jobs += [('coin', (f'"{base_asset(s)}" (crypto OR cryptocurrency OR token OR coin OR blockchain)',), s) for s in symbols]
    def run(job):
        tier, queries, symbol = job
        feed = fetch_tier(tier, queries, start, last_day, live, refresh, key=f'coin_{symbol}' if symbol else None)
        return {**feed, 'symbol': symbol} if symbol else feed
    with ThreadPoolExecutor(max_workers=6) as pool: feeds = list(pool.map(run, jobs))
    seen = set(); headlines = []; daily = {'major': {}, 'market': {}, 'coin': {}}; missing = []
    days = [(start + timedelta(days=i)).isoformat() for i in range((last_day - start).days + 1)]
    for feed in feeds:
        tier = feed['tier']; symbol = feed.get('symbol')
        ledger.fetch(feed, f"Recent {feed['label'].lower()}{' · ' + symbol if symbol else ''} discovery", 'news_discovery', bool(feed.get('items')))
        if feed.get('error') or not feed.get('items'): missing.append(feed['label'] + (f' · {symbol}' if symbol else ''))
        counts = {d: 0 for d in days}; per_day = {}; kept = []
        for item in sorted(feed.get('items', []), key=lambda row: (row['published_at'][:10], row.get('rank', 0))):
            key = norm(item['title'])
            if key in seen: continue   # first (heavier) tier keeps a duplicated story
            seen.add(key); day = item['published_at'][:10]
            counts[day] = counts.get(day, 0) + 1
            # Sample the top-ranked stories of every day so older days stay visible.
            if per_day.get(day, 0) < TIERS[tier]['per_day']:
                per_day[day] = per_day.get(day, 0) + 1; kept.append(item)
        if symbol: daily['coin'][symbol] = counts
        else: daily[tier] = counts
        for item in sorted(kept, key=lambda row: row['published_at'], reverse=True):
            age = (ref - datetime.fromisoformat(item['published_at'])).total_seconds() / 86400
            headlines.append({'tier_hint': tier, **({'symbol': symbol} if symbol else {}), 'title': item['title'],
                              'publisher': item['publisher'], 'published_at': item['published_at'],
                              'age_days': round(age, 2), 'freshness': freshness(age), 'source_id': ledger.headline(item)})
    return {'window_start': str(start), 'window_end': str(last_day), 'live': live, 'reference_time': ref.isoformat(),
            'half_life_days': HALF_LIFE_DAYS, 'tier_weights': {k: v['weight'] for k, v in TIERS.items()},
            'momentum_persistence': PERSISTENCE, 'daily_counts': daily, 'missing': missing, 'headlines': headlines,
            'note': 'Headline metadata only (articles not fetched). Market-wide tiers are limited to established newsrooms and crypto trade press; counts are from a capped search index, not complete coverage.'}


def narrative_schema():
    return {'type': 'array', 'items': {'type': 'object', 'properties': {
        'tier': {'type': 'string', 'enum': list(TIERS)}, 'title': {'type': 'string'},
        'rating': {'type': 'integer', 'enum': list(RATING)}, 'momentum': {'type': 'string', 'enum': list(PERSISTENCE)},
        'outlook': {'type': 'integer', 'enum': list(RATING)}, 'reason': {'type': 'string'},
        'source_ids': {'type': 'array', 'items': {'type': 'string'}}},
        'required': ['tier', 'title', 'rating', 'momentum', 'outlook', 'reason', 'source_ids'], 'additionalProperties': False}}


def weighted(rows, rating_key, weight_key):
    total = sum(r[weight_key] for r in rows)
    if not rows or total <= 0: return None
    return round(50 * sum(r[weight_key] * r[rating_key] for r in rows) / (total + NEUTRAL_ANCHOR))


def score(narratives, recent, symbol):
    """Validate one instrument's narratives and compute its recent pulse (now + next few days)."""
    if not recent: return None
    if not isinstance(narratives, list) or len(narratives) > MAX_NARRATIVES:
        raise ValueError(f'Recent news needs a list of at most {MAX_NARRATIVES} narratives')
    allowed = {h['source_id']: h for h in recent.get('headlines', [])
               if h.get('source_id') and (h['tier_hint'] != 'coin' or h.get('symbol') == symbol)}
    rows = []
    for row in narratives:
        if not isinstance(row, dict) or row.get('tier') not in TIERS or row.get('momentum') not in PERSISTENCE:
            raise ValueError('Invalid recent-news narrative tier or momentum')
        for key in ('rating', 'outlook'):
            if type(row.get(key)) is not int or row[key] not in RATING: raise ValueError('Narrative ratings must be integers from -2 to +2')
        for key in ('title', 'reason'):
            if not isinstance(row.get(key), str) or not row[key].strip(): raise ValueError('Each narrative needs a title and rationale')
        ids = row.get('source_ids')
        if not isinstance(ids, list) or not ids or any(i not in allowed for i in ids):
            raise ValueError('A narrative must cite recent headlines supplied for this instrument')
        newest = min(allowed[i]['age_days'] for i in ids)
        fresh = freshness(newest); weight = TIERS[row['tier']]['weight']
        rows.append({k: row[k] for k in ('tier', 'title', 'rating', 'momentum', 'outlook', 'reason', 'source_ids')} |
                    {'title': row['title'][:160], 'newest_age_days': round(newest, 2), 'freshness': fresh,
                     'weight_now': round(weight * fresh, 3), 'weight_next': round(weight * fresh * PERSISTENCE[row['momentum']], 3)})
    if sum(r['tier'] == 'major' for r in rows) > MAX_MAJOR: raise ValueError(f'At most {MAX_MAJOR} market-moving narratives')
    now, nxt = weighted(rows, 'rating', 'weight_now'), weighted(rows, 'outlook', 'weight_next')
    return {'narratives': rows, 'score_now': now, 'label_now': label(now), 'score_next': nxt, 'label_next': label(nxt),
            'window_start': recent['window_start'], 'window_end': recent['window_end'], 'live': recent['live'],
            'rubric': f"Narrative weight now = tier (market-moving {TIERS['major']['weight']}, broad market {TIERS['market']['weight']}, coin {TIERS['coin']['weight']}) × freshness (half-life {HALF_LIFE_DAYS:g} days from the newest cited headline). Next few days uses the AI's outlook rating with weight × momentum persistence (building {PERSISTENCE['building']}, steady {PERSISTENCE['steady']}, fading {PERSISTENCE['fading']}). Score = round(50 × Σ weight × rating ÷ (Σ weight + {NEUTRAL_ANCHOR:g} neutral anchor)). AI-rated headline tone, not a price forecast."}


def blend(monthly, pulse):
    """Equal blend of the monthly tone index and the recent pulse; either alone if the other is missing."""
    def mix(a, b):
        if a is None and b is None: return None
        return b if a is None else a if b is None else round((a + b) / 2)
    now = mix(monthly, pulse and pulse['score_now']); nxt = mix(monthly, pulse and pulse['score_next'])
    drift = None if now is None or nxt is None else 'improving' if nxt - now >= 10 else 'deteriorating' if nxt - now <= -10 else 'stable'
    return {'score_now': now, 'label_now': label(now), 'score_next': nxt, 'label_next': label(nxt), 'drift': drift,
            'rubric': 'Current = average of the monthly tone index and the recent news pulse; next few days = average of the monthly index and the projected pulse.'}
