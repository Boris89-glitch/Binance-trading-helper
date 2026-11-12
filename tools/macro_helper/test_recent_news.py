"""Recent tiered news pulse: window, decay, tier weights, citation scope and blending."""
import copy
from datetime import date, datetime, timezone
import json
import unittest
from unittest.mock import patch
from . import recent_news
from .provenance import Ledger
from .sentiment import schema, validate_pair_output
from .test_analysis import SUMMARY

NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)


def recent():
    def h(sid, tier, age, symbol=None):
        return {'tier_hint': tier, **({'symbol': symbol} if symbol else {}), 'title': sid, 'publisher': 'P',
                'published_at': '2026-11-01T00:00:00+00:00', 'age_days': age, 'freshness': recent_news.freshness(age), 'source_id': sid}
    return {'window_start': '2026-10-29', 'window_end': '2026-11-05', 'live': True, 'reference_time': NOW.isoformat(),
            'headlines': [h('S1', 'major', 6.0), h('S2', 'major', 0.5), h('S3', 'market', 1.0),
                          h('S4', 'coin', 0.2, 'BNBUSDT'), h('S5', 'coin', 0.2, 'ETHUSDT')]}


def narrative(tier, rating, momentum, outlook, ids):
    return {'tier': tier, 'title': 'Story', 'rating': rating, 'momentum': momentum, 'outlook': outlook, 'reason': 'Because.', 'source_ids': ids}


class RecentNewsChecks(unittest.TestCase):
    def test_window_is_seven_days_and_live_reports_include_today(self):
        self.assertEqual(recent_news.window(date(2026, 11, 4), date(2026, 11, 5)), (date(2026, 10, 29), date(2026, 11, 5), True))
        self.assertEqual(recent_news.window(date(2026, 9, 30), date(2026, 11, 5)), (date(2026, 9, 24), date(2026, 9, 30), False))

    def test_queries_stay_short_enough_for_google_date_filters(self):
        # Longer queries make Google News ignore after:/before:, returning only the latest days.
        for tier in ('major', 'market'):
            self.assertTrue(all(len(q) <= 150 for q in recent_news.TIERS[tier]['queries']))

    def test_freshness_halves_every_three_days(self):
        self.assertEqual(recent_news.freshness(0), 1.0)
        self.assertEqual(recent_news.freshness(3), 0.5)
        self.assertEqual(recent_news.freshness(6), 0.25)

    def test_fading_hack_and_building_optimism_are_weighted_by_tier_freshness_and_momentum(self):
        rows = [narrative('major', -2, 'fading', -1, ['S1']), narrative('major', 1, 'building', 1, ['S2'])]
        pulse = recent_news.score(rows, recent(), 'BNBUSDT')
        old, new = pulse['narratives']
        self.assertEqual(old['weight_now'], round(3 * 0.25, 3)); self.assertEqual(old['weight_next'], round(3 * 0.25 * 0.4, 3))
        f = recent_news.freshness(0.5)
        self.assertEqual(new['weight_now'], round(3 * f, 3))
        now = round(50 * (old['weight_now'] * -2 + new['weight_now'] * 1) / (old['weight_now'] + new['weight_now'] + 1))
        self.assertEqual(pulse['score_now'], now)
        self.assertGreater(pulse['score_next'], pulse['score_now'])

    def test_heavier_tier_dominates_lighter_tier(self):
        pulse = recent_news.score([narrative('major', 2, 'steady', 2, ['S3']), narrative('coin', -2, 'steady', -2, ['S4'])], recent(), 'BNBUSDT')
        self.assertGreater(pulse['score_now'], 0)

    def test_thin_coverage_is_damped_by_neutral_anchor(self):
        pulse = recent_news.score([narrative('coin', 2, 'steady', 2, ['S4'])], recent(), 'BNBUSDT')
        self.assertLess(pulse['score_now'], 60)
        self.assertIsNone(recent_news.score([], recent(), 'BNBUSDT')['score_now'])

    def test_narratives_cite_only_shared_tiers_or_own_coin_headlines(self):
        with self.assertRaisesRegex(ValueError, 'cite'):
            recent_news.score([narrative('coin', 1, 'steady', 1, ['S5'])], recent(), 'BNBUSDT')
        with self.assertRaisesRegex(ValueError, 'cite'):
            recent_news.score([narrative('coin', 1, 'steady', 1, ['S999'])], recent(), 'BNBUSDT')
        with self.assertRaisesRegex(ValueError, 'cite'):
            recent_news.score([narrative('coin', 1, 'steady', 1, [])], recent(), 'BNBUSDT')

    def test_invalid_ratings_and_too_many_major_narratives_are_rejected(self):
        for bad in (narrative('major', 3, 'steady', 0, ['S2']), narrative('major', 0, 'peaking', 0, ['S2']), narrative('macro', 0, 'steady', 0, ['S2'])):
            with self.assertRaises(ValueError): recent_news.score([bad], recent(), 'BNBUSDT')
        with self.assertRaisesRegex(ValueError, 'At most'):
            recent_news.score([narrative('major', 0, 'steady', 0, ['S2'])] * 4, recent(), 'BNBUSDT')

    def test_blend_averages_monthly_index_with_pulse_and_reports_drift(self):
        pulse = {'score_now': -40, 'score_next': 10}
        combined = recent_news.blend(20, pulse)
        self.assertEqual((combined['score_now'], combined['score_next'], combined['drift']), (-10, 15, 'improving'))
        self.assertEqual(recent_news.blend(None, pulse)['score_now'], -40)
        self.assertEqual(recent_news.blend(30, None)['score_now'], 30)

    def test_pair_output_carries_pulse_and_schema_requires_narratives(self):
        self.assertIn('narratives', schema(['BNBUSDT'])['properties']['pairs']['items']['required'])
        self.assertNotIn('narratives', schema()['properties'])
        sources = [{'id': f'S{i}', 'available': True} for i in range(1, 6)]
        pair = {'symbol': 'BNBUSDT', **copy.deepcopy(SUMMARY), 'narratives': [narrative('major', -2, 'fading', -1, ['S1'])]}
        raw = {**copy.deepcopy(SUMMARY), 'pairs': [pair]}
        result = validate_pair_output(json.dumps(raw), [{'symbol': 'BNBUSDT', 'available': True}], sources, recent())['pairs'][0]
        self.assertLess(result['news_pulse']['score_now'], 0)
        self.assertEqual(result['combined']['score_now'], round((result['score'] + result['news_pulse']['score_now']) / 2))
        self.assertNotIn('news_pulse', validate_pair_output(json.dumps(raw), [{'symbol': 'BNBUSDT', 'available': True}], sources)['pairs'][0])

    def test_collect_dedupes_stories_into_heaviest_tier_and_counts_by_day(self):
        def item(title, day):
            return {'title': title, 'url': f'https://news.example.test/{abs(hash(title))}', 'published_at': f'{day}T10:00:00+00:00', 'publisher': 'Fixture', 'publisher_url': 'https://www.reuters.com'}
        feeds = {'major': [item('Binance hack - Reuters', '2026-10-30'), item('Midterms lift crypto - Reuters', '2026-11-04')],
                 'market': [item('Binance hack - CNBC', '2026-10-30'), item('Crypto market rebounds', '2026-11-04')],
                 'coin': [item('BNB burn', '2026-11-03')]}
        def fake(tier, query, start, last_day, live, refresh=False, key=None):
            return {'tier': tier, 'label': recent_news.TIERS[tier]['label'], 'items': feeds[tier], 'request_log': [], 'cached': False}
        with patch.object(recent_news, 'fetch_tier', side_effect=fake):
            block = recent_news.collect(['BNBUSDT'], date(2026, 11, 4), Ledger(), now=NOW)
        tiers = [(h['tier_hint'], h['title']) for h in block['headlines']]
        self.assertIn(('major', 'Binance hack - Reuters'), tiers); self.assertNotIn(('market', 'Binance hack - CNBC'), tiers)
        self.assertEqual(block['daily_counts']['major']['2026-10-30'], 1); self.assertEqual(block['daily_counts']['coin']['BNBUSDT']['2026-11-03'], 1)
        self.assertTrue(block['live']); self.assertEqual(block['window_end'], '2026-11-05')
        self.assertTrue(all(h['source_id'] for h in block['headlines']))


if __name__ == '__main__': unittest.main()
