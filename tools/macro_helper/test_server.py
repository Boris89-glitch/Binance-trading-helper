"""Financial calculation and window-boundary checks; no network calls."""
from datetime import date
import unittest
from unittest.mock import patch
from .server import correlate, parse_window, returns, summarize, news, monthly_report, RESULTS, SERIES


class ResearchChecks(unittest.TestCase):
    def series(self, points, unit='percent'):
        return {'key':'btc', 'points':points, 'unit':unit}

    def test_window_uses_preceding_close_and_excludes_next_month(self):
        s=summarize(self.series([('2024-12-31',100),('2025-01-01',101),('2025-01-31',110),('2025-02-01',200)]),date(2025,1,1),date(2025,1,31))
        self.assertAlmostEqual(s['change'],10)
        self.assertEqual(s['last_date'],'2025-01-31')

    def test_no_baseline_is_missing_not_zero(self):
        s=summarize(self.series([('2025-01-01',101)]),date(2025,1,1),date(2025,1,31))
        self.assertIsNone(s['change'])

    def test_yield_changes_are_basis_points(self):
        s=summarize(self.series([('2024-12-31',4),('2025-01-31',4.25)],'bp'),date(2025,1,1),date(2025,1,31))
        self.assertEqual(s['change'],25)

    def test_efficiency_distinguishes_round_trip(self):
        points=[('2024-12-31',100),('2025-01-01',110),('2025-01-02',100)]
        s=summarize(self.series(points),date(2025,1,1),date(2025,1,31))
        self.assertEqual(s['efficiency'],0)
        self.assertEqual(s['coverage'],'partial / lagged')

    def test_monotone_efficiency_is_one(self):
        s=summarize(self.series([('2024-12-31',100),('2025-01-01',105),('2025-01-02',110)]),date(2025,1,1),date(2025,1,2))
        self.assertEqual(s['efficiency'],1)

    def test_correlation_requires_sample_and_handles_constant(self):
        self.assertEqual(correlate({'a':1},{'a':1}),(None,1))
        self.assertEqual(correlate({str(i):1 for i in range(10)},{str(i):i for i in range(10)}),(None,10))
        corr,n=correlate({str(i):i for i in range(10)},{str(i):-i for i in range(10)})
        self.assertAlmostEqual(corr,-1)
        self.assertEqual(n,10)

    def test_returns_do_not_fill_holidays(self):
        r=returns([('2025-01-03',100),('2025-01-06',110)],date(2025,1,1),date(2025,1,31))
        self.assertEqual(list(r),['2025-01-06'])

    def test_supplied_partial_month_cutoff(self):
        _,_,start,end=parse_window({'month':['2026-10']})
        self.assertEqual((start,end),(date(2026,10,1),date(2026,10,17)))

    def test_previous_month_crosses_year(self):
        _,_,start,end=parse_window({'month':['2025-01'],'mode':['blind']})
        self.assertEqual((start,end),(date(2024,12,1),date(2024,12,31)))

    def test_reject_unsupported_window(self):
        with self.assertRaises(ValueError):parse_window({'month':['2024-01']})
        with self.assertRaises(ValueError):parse_window({'mode':['bogus']})

    def test_preserves_reported_and_rounded_totals(self):
        self.assertEqual(sum(RESULTS['2025']['pnl']),9814)
        self.assertEqual(sum(RESULTS['2026']['pnl']),5212)
        self.assertEqual(RESULTS['2026']['reported_pnl'],5211)

    def test_news_rejects_wrong_dates_domains_and_irrelevant_titles(self):
        def item(title,host,published):
            return f'<item><title>{title}</title><link>https://news.google.com/story</link><pubDate>{published}</pubDate><source url="https://{host}">Publisher</source></item>'
        xml='<rss><channel>'+''.join([
            item('Oil supply outlook','eia.gov','Mon, 02 Jun 2025 12:00:00 GMT'),
            item('Oil supply','eia.gov.evil.example','Mon, 02 Jun 2025 12:00:00 GMT'),
            item('Oil supply','eia.gov','Fri, 02 May 2025 12:00:00 GMT'),
            item('Generic homepage','eia.gov','Mon, 02 Jun 2025 12:00:00 GMT'),
        ])+'</channel></rss>'
        with patch('tools.macro_helper.server.get_url',return_value=xml), patch('tools.macro_helper.server.cached',side_effect=lambda key,fetch,refresh:fetch()):
            result=news('oil',date(2025,6,1),date(2025,6,30))
        self.assertEqual(len(result['items']),1)
        self.assertEqual(result['items'][0]['title'],'Oil supply outlook')

    def test_blind_mode_never_fetches_cpi_or_exposes_result_cards(self):
        def fixture(key,start,end,refresh):
            return {'key':key,'label':SERIES[key][0],'unit':SERIES[key][3], 'points':[], 'source':'fixture'}
        with patch('tools.macro_helper.server.fetch_series',side_effect=fixture) as fetch:
            report=monthly_report({'month':['2025-01'],'mode':['blind']})
        self.assertNotIn('cpi',[call.args[0] for call in fetch.call_args_list])
        self.assertNotIn('Seasonality: same month, different years',[e['topic'] for e in report['evidence']])
        self.assertEqual(report['start'],'2024-12-01')

    def test_profitable_choppy_month_surfaces_counterevidence(self):
        def fixture(key,start,end,refresh):
            return {'key':key,'label':SERIES[key][0],'unit':SERIES[key][3], 'points':[['2026-09-30',100],['2026-10-01',110],['2026-10-17',101]], 'source':'fixture'}
        with patch('tools.macro_helper.server.fetch_series',side_effect=fixture):
            report=monthly_report({'month':['2026-10']})
        card=next(e for e in report['evidence'] if e['topic']=='Strategy outcome vs BTC regime')
        self.assertIn('counterevidence',card['support'])


if __name__=='__main__':unittest.main()
