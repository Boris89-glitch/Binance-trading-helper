"""Blind AI scoring of each headline, once, cached in scores.jsonl (resumable).

Blinding: the model never sees dates, publishers or numbers (all masked as [N]), and is told to
judge each headline as if read at publication time. It classifies every headline as an event,
a market reaction (prices, flows, predictions) or irrelevant; only events feed the signal.
The hindsight check re-scores a sample with well-known entities replaced by generic roles."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import random
import re
import sys
import tempfile
import threading

from .common import DATA, append_jsonl, read_jsonl

BATCH = 120
SCORES = DATA / 'scores.jsonl'
MASKED_SCORES = DATA / 'scores_entity_masked.jsonl'
LOCK = threading.Lock()
PROMPT = '''You classify crypto news headlines for a research study.
Headlines have NO dates and numbers are masked as [N]. Judge each headline only by its
own wording, as a reader would at the moment it was published. Do not use any knowledge
of later events, outcomes or prices; if you recognise a story, ignore what followed.
For every headline return id, category, tier and rating:
- category: "event" = something happened or was decided (hack, exploit, theft, failure,
  lawsuit, enforcement, regulation, legislation, election or political action, central
  bank or macro policy decision, sanctions, product launch, adoption, corporate or
  treasury action, partnership, network upgrade, outage); "market_reaction" = the
  headline mainly reports or predicts market behaviour (price moves, performance,
  rallies, selloffs, technical levels, liquidations, fund or ETF flows, volumes,
  sentiment indices, analyst targets or forecasts); "irrelevant" = not about crypto
  or carries no usable information (listicles, ads, explainers, how-tos).
- tier: "major" = could move the whole crypto market (large exchange hack or failure,
  government or regulator action, elections, landmark legislation, macro shock);
  "market" = broad crypto-industry news; "coin" = specific to Bitcoin.
- rating: integer -2..+2, the tone of the headline for Bitcoin's outlook as written
  (-2 clearly negative, 0 neutral/unclear, +2 clearly positive). Rate market_reaction
  headlines by the tone they report. Rate irrelevant headlines 0.
Return exactly one entry per supplied id.'''
NUMBER = re.compile(r'(?:US)?\$\s?\d[\d,.]*\s?(?:k|m|bn|b|billion|million|trillion)?\b|\b\d[\d,.]*\s?(?:%|percent|k\b|bn\b|billion|million|trillion|x\b)?', re.I)
DATEWORD = re.compile(r'\b(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sep(t(ember)?)?|oct(ober)?|nov(ember)?|dec(ember)?|'
                      r'monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|yesterday|tomorrow|this week|last week|q[1-4])\b\.?', re.I)
ENTITIES = [  # hindsight check only: well-known names -> generic roles
    (r'\bBinance\b|\bCoinbase\b|\bKraken\b|\bOKX\b|\bBybit\b|\bBitfinex\b|\bGemini\b|\bRobinhood\b|\bFTX\b|\bHyperliquid\b', 'a crypto exchange'),
    (r'\bBlackRock\b|\bFidelity\b|\bGrayscale\b|\bVanEck\b|\bARK\b|\bJPMorgan\b|\bGoldman( Sachs)?\b|\bCitadel\b|\bMorgan Stanley\b', 'a large financial firm'),
    (r'\b(Micro)?Strategy\b|\bTesla\b|\bMetaplanet\b|\bCircle\b|\bTether\b|\bRipple\b', 'a crypto-linked company'),
    (r'\bTrump\b|\bBiden\b|\bHarris\b|\bVance\b|\bBessent\b|\bLutnick\b|\bWarren\b|\bLummis\b', 'a politician'),
    (r'\bPowell\b|\bWarsh\b|\bHassett\b|\bGensler\b|\bAtkins\b|\bPeirce\b', 'an official'),
    (r'\bSaylor\b|\bMusk\b|\bCZ\b|\bChangpeng Zhao\b|\bArmstrong\b|\bSBF\b|\bBankman-Fried\b|\bDo Kwon\b', 'an industry figure'),
    (r'\bNorth Korea(n)?\b|\bLazarus\b|\bRussia(n)?\b|\bChina\b|\bChinese\b|\bIran(ian)?\b', 'a foreign state'),
    (r'\bDemocrat(s|ic)?\b|\bRepublican(s)?\b|\bGOP\b', 'a party'),
    (r'\bSEC\b|\bCFTC\b|\bOCC\b|\bTreasury\b|\bDOJ\b|\bFBI\b', 'a US agency'),
    (r'\bFed\b|\bFederal Reserve\b|\bFOMC\b', 'the central bank'),
    (r'\bGENIUS Act\b|\bCLARITY Act\b|\bClarity Act\b', 'a crypto bill'),
]


def blind(title, entities=False):
    text = re.sub(r'\s+-\s+[^-]+$', '', title)             # publisher suffix
    text = DATEWORD.sub('[DATE]', NUMBER.sub('[N]', text))
    if entities:
        for pattern, role in ENTITIES: text = re.sub(pattern, role, text)
    return ' '.join(text.split())


def schema(ids):
    return {'type': 'object', 'additionalProperties': False, 'required': ['ratings'], 'properties': {'ratings': {
        'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'required': ['id', 'category', 'tier', 'rating'], 'properties': {
            'id': {'type': 'string', 'enum': ids}, 'category': {'type': 'string', 'enum': ['event', 'market_reaction', 'irrelevant']},
            'tier': {'type': 'string', 'enum': ['major', 'market', 'coin']}, 'rating': {'type': 'integer', 'enum': [-2, -1, 0, 1, 2]}}}}}}


def ask(batch, entities=False):
    """One Codex CLI call (ChatGPT subscription, read-only sandbox, no tools); returns validated rows."""
    from tools.macro_helper.codex_model import executable, run, status
    if not status()['configured']: raise RuntimeError('Codex must be signed in with ChatGPT (codex login)')
    ids = [h['id'] for h in batch]
    lines = '\n'.join(f'{h["id"]}\t{blind(h["title"], entities)}' for h in batch)
    prompt = PROMPT + '\nUse no tools, files or web search. Return only the required JSON.\nHeadlines (id<TAB>text):\n' + lines
    with tempfile.TemporaryDirectory(prefix='news-study-') as folder:
        directory = Path(folder); schema_path = directory / 'schema.json'; output = directory / 'out.json'
        schema_path.write_text(json.dumps(schema(ids)), encoding='utf-8')
        args = [executable(), 'exec', '--ignore-user-config', '--ephemeral', '--skip-git-repo-check', '--sandbox', 'read-only',
                '--model', 'gpt-6.1-sol', '--disable', 'shell_tool', '--disable', 'multi_agent', '--disable', 'enable_mcp_apps',
                '-c', 'web_search="disabled"', '-c', 'mcp_servers={}', '-c', 'model_reasoning_effort="low"',
                '--output-schema', str(schema_path), '--output-last-message', str(output), '-']
        result = run(args, input=prompt, cwd=directory, timeout=420)
        if result.returncode != 0 or not output.is_file(): raise RuntimeError('Codex call failed: ' + result.stderr[-300:])
        rows = json.loads(output.read_text(encoding='utf-8'))['ratings']
    got = {r['id']: r for r in rows if r['id'] in ids}
    if len(got) < len(ids) * 0.9: raise RuntimeError(f'Codex returned {len(got)}/{len(ids)} ratings')
    return [{'id': i, **{k: got[i][k] for k in ('category', 'tier', 'rating')}} for i in ids if i in got]


def pending(path, rows):
    done = {r['id'] for r in read_jsonl(path)}
    return [h for h in rows if h['id'] not in done]


def score_all(rows, path=SCORES, entities=False, workers=3):
    todo = pending(path, rows)
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    print(f'scoring {len(todo)} headlines in {len(batches)} calls ({path.name})', flush=True)
    def work(item):
        n, batch = item
        for attempt in range(3):
            try:
                out = ask(batch, entities)
                with LOCK: append_jsonl(path, out)
                print(f'  batch {n + 1}/{len(batches)} ok ({len(out)})', flush=True); return
            except Exception as exc:
                print(f'  batch {n + 1} attempt {attempt + 1} failed: {str(exc)[:200]}', flush=True)
        print(f'  batch {n + 1} skipped; rerun to retry', flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool: list(pool.map(work, enumerate(batches)))


def hindsight_sample(n=480, seed=7):
    """Headlines that name a well-known entity, for the masked re-score."""
    rows = json.loads((DATA / 'headlines.json').read_text(encoding='utf-8'))
    named = [h for h in rows if blind(h['title'], True) != blind(h['title'])]
    random.Random(seed).shuffle(named)
    return named[:n]


if __name__ == '__main__':
    headlines = json.loads((DATA / 'headlines.json').read_text(encoding='utf-8'))
    if 'hindsight' in sys.argv: score_all(hindsight_sample(), MASKED_SCORES, entities=True)
    elif '--sample' in sys.argv: score_all(headlines[:int(sys.argv[sys.argv.index('--sample') + 1])], workers=1)
    else: score_all(headlines)
