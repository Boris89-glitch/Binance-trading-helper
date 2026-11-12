# Crypto helper

A small set of personal research tools for crypto markets. Nothing here gives trading advice. The trading desk connects only to the Binance Futures **testnet**.

## Contents

| Path | What it is |
| --- | --- |
| [`tools/macro_helper`](tools/macro_helper/README.md) | Local web app for monthly AI sentiment reports (macro and per-coin), a recent-news pulse, a trade archive and a testnet trading desk. |
| `tools/news_study` | Offline study testing whether blind, price-stripped AI news sentiment leads BTC price. Also tests the Fear & Greed Index. |
| `tools/trend_study` | BTC 4h moving-average hold filter compared with buy & hold. |

## Quick start

Requires Python 3.10+. The macro helper runs on the standard library alone; the trading desk also needs `python-dotenv`.

```powershell
python -m tools.macro_helper
```

Then open http://127.0.0.1:8765, or run `start.bat`, which opens the browser and restarts the server when asked. In AI settings, pick a provider: Google Gemini (free key), OpenAI through a Codex CLI login, or the paid Anthropic API.

The study scripts also need `numpy` and `scipy`:

```powershell
pip install numpy scipy
python -m tools.news_study.backfill
python -m tools.news_study.score
python -m tools.news_study.evaluate
python -m tools.news_study.fear_greed
python -m tools.trend_study.ma_hold
```

## Local data

The app keeps settings (including API keys), report archives, trades and study caches in `.macro_helper_local/` and `.macro_helper_cache/`. Both are gitignored, so keep them private.

## Tests

```powershell
python -m unittest discover -s tools/macro_helper -p "test_*.py" -t .
```

See the [macro helper README](tools/macro_helper/README.md) for full details on the sentiment score, sources and provenance, and the trading desk.
