# Monthly sentiment

```powershell
python -m tools.macro_helper
```

Open http://127.0.0.1:8765, choose a month and view, then load macro AI. Pick a crypto under the macro report and analyze it, or use **Refresh macro + selected** to refresh both reports in sequence. Pair reports use the same month and view as macro; **Before the month** uses only the prior month. `--port 8766` changes the port; Ctrl+C stops the server. Python 3.10+ and the standard library are sufficient. The trading bot and exchange credentials are never imported.

This helper lives in its own `crypto helper` project folder. It reads only testnet keys from the sibling `crypto` bot folder; its cache, settings and report archive stay inside this project.

## Sentiment, not a strategy verdict

New reports describe observed market tone in **3–4 paragraphs**, with a sentiment score and linked evidence. They do not estimate strategy performance, win rates, confidence probabilities, future returns, trade suitability or position sizes. Backtest results and strategy rules are no longer sent to the model.

Macro summaries and selected-crypto summaries have 3–4 paragraphs. The crypto dropdown lists your USDT-M favorites only; USDC pairs belong to the crypto bot. Each crypto report combines that coin's futures price data, macro context and coin-specific headlines, linked to the exact saved macro version; switching coins opens its saved report for the month if one exists.

## Exactly one observation month

- **During the month**: the selected month, through its end or the latest completed UTC day. Selecting September 2026 after it ends uses **September 1–30** only.
- **Before the month**: the immediately preceding month, through its end or the latest completed UTC day. Selecting September 2026 uses **August 1–31** only; selecting November on October 27 uses **October 1–26**.
- A previous closing observation is used solely as the baseline for month returns. Provider queries may request a short pre-window buffer to find that close; earlier monthly regimes are not given to the model. News dates are restricted to the same observation window, with no extra August 31 in a September report.
- Only completed UTC candles are retained. Latest/revised prices and metadata retrieved today are not historical point-in-time vintages. Later-generated past-month reports are labelled reconstructions.

## Sentiment score

This is an **AI-rated tone index**, not a calibrated probability, official market index or measured trading edge. Its inputs are observed prices and institutional headline metadata; the model interprets their tone.

The model must rate five topics: price tone, macro/policy tone, risk/volatility tone, Asia/global tone and headline tone. Each rating is `-2` clearly bearish, `-1` somewhat bearish, `0` neutral/mixed, `+1` somewhat bullish or `+2` clearly bullish. Missing evidence is `null`, excluded rather than treated as neutral. Every non-null rating requires an available source ID and a rationale.

The server computes `round(50 × mean(available ratings))`, requiring at least three rated topics. The model cannot supply or override the total. Below three topics, the score is unavailable. Labels: -100 to -60 very bearish; -59 to -20 bearish; -19 to +19 neutral/mixed; +20 to +59 bullish; +60 to +100 very bullish. Component ratings, rationales, source references and the formula are displayed alongside the total.

## Recent news pulse (crypto reports only)

Each crypto report also reads the **last 7 days of headlines** up to the data cutoff (a live report also includes today's headlines so far), in three tiers searched day by day so older days are not crowded out:

- **Market-moving events** (weight 3): exchange hacks/failures, elections and political shifts, landmark regulation, macro shocks. Established newsrooms and crypto trade press only.
- **Broad crypto market** (weight 2): aggregate market tone, flows and price commentary, same publisher filter.
- **Coin-specific** (weight 1): the selected coin's own headlines, unfiltered.

The model groups headlines into up to 8 narratives per coin (at most 3 market-moving), rating each one's tone for that coin now (−2…+2), its **momentum** (building / steady / fading, judged from daily headline counts and follow-ups) and its **outlook** for the next 3–5 days. The server computes:

- weight now = tier weight × freshness, where freshness halves every 3 days from the newest cited headline;
- weight next = weight now × momentum persistence (building 1.0, steady 0.7, fading 0.4);
- pulse = round(50 × Σ weight × rating ÷ (Σ weight + 1)); the +1 neutral anchor damps thin coverage;
- **current tone** = average of the monthly tone index and the pulse, **next few days** = average of the monthly index and the projected pulse, with drift improving/deteriorating at ±10.

The outlook is an AI-rated projection of headline sentiment, not a price forecast. The macro report is unchanged and stays macro-only. Narratives may cite only the shared tiers and that coin's own headlines.

## Source links and provenance

Every new report archives a source ledger, displayed in its Sources section and included in Markdown export:

- Exact public data request URLs, discovery/search URLs, redirects and failed requests made by the collector.
- Every headline URL actually supplied to the model, with publisher and publication date.
- Whether evidence was fetched for that run or reused from a cached snapshot, retaining its original retrieval timestamp.
- Whether a request failed or did not supply usable window evidence. Such sources cannot support a score rating.

**Headline metadata is not article text.** The app searches Google News RSS with a publisher-domain whitelist and relevance/date filters; it does not fetch full article bodies or claim that linked articles were visited. Headline entries explicitly say “article not fetched.” The model has no independent browsing/tools and may cite only source IDs in the supplied ledger. Source links are built by the app, not invented by the model. News coverage is an incomplete search index, not proof that unlisted events did not occur.

Sources cover Binance public crypto data, Yahoo Finance cross-market data, and institutional domains such as the Fed, BLS, BEA, BOJ, HKMA, PBOC, IMF, BIS, EIA, IEA, UN, EU Council, OFAC and Deribit. No Reuters/general-media feed is supplied. Inflation vintages/surprises, funding, options positioning, a reliable complete event calendar, token unlocks and asset-specific earnings are not loaded and must not be invented.

## Model and connection

**Google Gemini (free tier) is the default.** Create a free key at aistudio.google.com/apikey (no card), paste it in AI settings, and the helper calls **Gemini 3.8 Flash** through the Gemini API with JSON output; `GEMINI_API_KEY` in the environment also works. Free-tier limits are small (roughly 20 requests/day; one per macro or crypto refresh). If Gemini 3.8 Flash is overloaded (503), rate-limited (429) or times out, the request falls back to Gemini 2.5 Flash, then Gemini 3.5 Flash; the archived report records the model that answered, and on the free tier Google may use prompts to improve its products. The key is stored in ignored `.macro_helper_local/settings.json`.

OpenAI remains selectable, with no Anthropic key required. The selected OpenAI model is **GPT-6.1 Sol**, using medium reasoning through Codex CLI signed in with ChatGPT (`codex login`); the CLI bundled with the Codex desktop app is found automatically. It uses your plan's Codex allowance and limits; no OpenAI API key is required. Longer reports and the stronger model can consume more allowance. Codex runs ephemerally in a temporary directory with read-only sandbox, shell/multi-agent tools and web search disabled, no user configuration/MCP servers, and a strict structured-output schema. API-key environment variables are removed and API-only login is rejected, avoiding silent separate API billing.

Anthropic Haiku remains a manually selected, separately billed API alternative; it requires an Anthropic API key. There are no automatic provider fallbacks or model retries. Its output caps are 3,500 tokens for macro reports and 18,000 for full instrument batches; lengths and usage affect actual charges. Subscription reports have no API-dollar estimate. Actual successful-call token usage is archived.

## Archives and refresh

Reports and original input snapshots are saved in ignored `.macro_helper_local/analyses.sqlite3`. Settings (including any optional Anthropic key) are stored separately in ignored `.macro_helper_local/settings.json`. Keep that directory private and back it up to retain your archive.

New reports carry `sentiment-v1`; earlier strategy-format reports remain intact in the database and are excluded from the sentiment view. New output never rewrites them or fabricates missing historical links. The format version prevents an old strategy summary from being reused as a sentiment report.

Refresh refetches public inputs and always generates a new AI report, even when the evidence is unchanged, using plan allowance or API credit. This also applies to the first explicit analysis click; saved versions are loaded when opening a month or archive. Opening saved reports is free. Pair reports retain the exact parent macro version and require matching month, perspective and cutoff. The combined refresh action runs macro then the selected pair sequentially; other refresh controls are disabled while either request is running. A timeout may still consume plan allowance/API credit even when no result reaches the app.

## Trade archive and trading section

Desk fills remain in the same SQLite database, separate from sentiment inputs, and are not sent to AI. The table lists the current UTC month by default; filter/export by long/short side, instrument and UTC month.

The Trading section connects only to Binance Futures testnet (`https://demo-fapi.binance.com`) using `BINANCE_TEST_FUTURES_API_KEY` and `BINANCE_TEST_FUTURES_SECRET_KEY` from the sibling crypto project's `ggg.env` (or those environment variables). Production credentials and production endpoints are never used. Install `python-dotenv` if it is not already available. Credentials stay on the server.

Save named configurations for contract, risk dollars, ATR timeframe, stop/target multiples, adjustment risk and four keyboard shortcuts. Select a configuration to restore it; the browser remembers the last selected name. Defaults are $50 risk, Wilder ATR(14) on completed 30-minute candles, 2.5 ATR stop and 4 ATR target (1:1.6 risk/reward), and leverage chosen per entry like the crypto bot: the highest Binance bracket leverage whose notional cap (less 5%) fits the position. Settings and operation records live in `.macro_helper_local/trading.sqlite3`, separate from research and archived trade events.

The desk trades USDT-M favorites only: USDC contracts, balances and positions are hidden and rejected. Post-only limit buy/sell use the selected USDT contract. A blank price joins the best bid/ask; a price that would cross is rejected. Limits are plain manual entries: no stop-loss or take-profit is placed, before or after the fill. Size is still risk ÷ (stop ATR × ATR) at the limit price. A background watcher tracks fills while the helper runs. Fills on watched USDT contracts sync to the trade archive every 15 seconds while the helper runs, page open or not, including closes made directly on Binance.

Market buy/sell calculate quantity from risk divided by the ATR stop distance, rounding down to exchange increments. Stops and targets are anchored to the actual average fill and submitted as close-position market-trigger orders using mark price. If protection fails, the desk attempts an emergency reduce-only market close and explicitly reports an unconfirmed outcome. Risk excludes fees/slippage and treats the contract's USDC/USDT quote units as dollar units; it is not a guaranteed maximum loss. The desk uses the account's existing margin mode and requires one-way position mode without changing it automatically.

Select an existing position to top up, reduce, or add a stop-loss or take-profit; full closes happen on Binance. Set stop and Set target work individually for positions without one, such as filled limit entries: each takes its own ATR multiple and timeframe, anchors to the average entry, and places a close-position mark-price order for the whole position. A level already past the mark price is rejected, an existing stop or target is not replaced, and a protected position takes no further limits. Top-ups add dollars of risk measured from the new market entry to its existing stop. Reductions remove dollars of open-position risk measured from average entry to stop; realized P&L is separate. Stops retain their prices. Without a single full-position stop, top-up and reduce size by risk ÷ (stop ATR × ATR), like entries. A position with no stop or target accepts up to 3 working post-only limits per contract (same-side limits add; opposite-side limits are reduce-only, capped at the position size). A fixed-quantity mark-price stop is supplemented with a close-position stop before top-up. Existing external take-profit orders are not resized. A profit-locking stop cannot be used to calculate a loss-risk reduction. Reductions never reverse the position. Avoid simultaneously operating a bot and the desk on the same contract: external position changes cannot be made atomic with a manual order.

Shortcuts are on by default; they pause while the window is unfocused or hidden and resume on return. Escape or unticking turns them off until re-enabled. They work only while Trading is visible (also with a dropdown focused), ignore typing, modifiers and repeated keydown events, and share the same guarded handlers as buttons. Every order, from a button or shortcut, opens a confirmation prompt first: Enter confirms, Escape cancels. Balances, positions and the trade archive sync refresh on load, when the tab becomes visible, after each action and via Refresh; unavailable account state disables trading. Every action re-checks positions and orders on the server before sending. Durable request IDs prevent replaying the same action. Uncertain outcomes block new exposure until Reconcile checks the original exchange order; market requests are never automatically retried. Operation recovery survives a server restart.

## Checks

```powershell
python -m unittest tools.macro_helper.test_server tools.macro_helper.test_analysis tools.macro_helper.test_workspace tools.macro_helper.test_codex_model tools.macro_helper.test_gemini_model tools.macro_helper.test_sentiment tools.macro_helper.test_trading tools.macro_helper.test_recent_news
```

Tests include one-month boundaries, exclusion of backtests/future observations, 3–4 paragraph validation, rating/score arithmetic, missing evidence, fabricated citations, cache/redirect/failure audit logs, headline-vs-article access labels, archive integrity and subscription isolation. Unit tests do not consume model allowance or API credit.
