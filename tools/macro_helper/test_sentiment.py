"""One-month evidence, auditable requests and reproducible sentiment scoring."""
from datetime import date
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError
from . import analysis,server
from .provenance import Ledger
from .sentiment import validate_output
from .test_analysis import RAW_SUMMARY,SOURCES


class SentimentChecks(unittest.TestCase):
    def test_september_monitor_is_september_and_outlook_is_august_only(self):
        self.assertEqual(analysis.forecast_window('2026-09',date(2026,10,27),'monitor')[1:],(date(2026,9,1),date(2026,9,30)))
        self.assertEqual(analysis.forecast_window('2026-09',date(2026,10,27),'outlook')[1:],(date(2026,8,1),date(2026,8,31)))

    def test_score_is_computed_from_ratings_not_model_total(self):
        raw=copy.deepcopy(RAW_SUMMARY);raw['score']=100
        for row,rating in zip(raw['components'],[-2,-1,0,1,2]):row['rating']=rating
        result=validate_output(json.dumps(raw),SOURCES)
        self.assertEqual(result['score'],0);self.assertEqual(result['sentiment'],'neutral')
        for row in raw['components']:row['rating']=-1
        result=validate_output(json.dumps(raw),SOURCES)
        self.assertEqual(result['score'],-50);self.assertEqual(result['sentiment'],'bearish')

    def test_missing_topics_are_excluded_rather_than_counted_as_neutral(self):
        raw=copy.deepcopy(RAW_SUMMARY)
        for row in raw['components']:row['rating']=1
        raw['components'][-1].update(rating=None,source_ids=[])
        self.assertEqual(validate_output(json.dumps(raw),SOURCES)['score'],50)
        for row in raw['components'][2:]:row.update(rating=None,source_ids=[])
        result=validate_output(json.dumps(raw),SOURCES)
        self.assertIsNone(result['score']);self.assertEqual(result['sentiment'],'insufficient')

    def test_fabricated_or_unavailable_citations_are_rejected(self):
        raw=copy.deepcopy(RAW_SUMMARY);raw['components'][0]['source_ids']=['S999']
        with self.assertRaisesRegex(ValueError,'unknown'):validate_output(json.dumps(raw),SOURCES)
        raw=copy.deepcopy(RAW_SUMMARY);raw['paragraphs'][0]+=' [S999]'
        with self.assertRaisesRegex(ValueError,'unknown'):validate_output(json.dumps(raw),SOURCES)
        with self.assertRaisesRegex(ValueError,'unavailable'):validate_output(json.dumps(RAW_SUMMARY),[{**SOURCES[0],'available':False}])

    def test_no_rated_topic_without_evidence_and_no_missing_or_duplicate_topics(self):
        raw=copy.deepcopy(RAW_SUMMARY);raw['components'][0]['source_ids']=[]
        with self.assertRaises(ValueError):validate_output(json.dumps(raw),SOURCES)
        raw=copy.deepcopy(RAW_SUMMARY);raw['components'][0]['rating']=True
        with self.assertRaises(ValueError):validate_output(json.dumps(raw),SOURCES)
        raw=copy.deepcopy(RAW_SUMMARY);raw['components'][0]['key']=raw['components'][1]['key']
        with self.assertRaises(ValueError):validate_output(json.dumps(raw),SOURCES)

    def test_paragraph_count_is_three_or_four(self):
        for count in (0,1,2,5):
            raw={**RAW_SUMMARY,'paragraphs':['Text [S1].']*count}
            with self.assertRaises(ValueError):validate_output(json.dumps(raw),SOURCES)
        self.assertEqual(len(validate_output(json.dumps({**RAW_SUMMARY,'paragraphs':['Text [S1].']*4}),SOURCES)['paragraphs']),4)

    def test_news_and_price_metric_window_have_same_month_boundaries(self):
        calls=[]
        def prices(key,start,end,refresh=False):
            calls.append(('prices',start,end))
            return {'key':key,'label':key,'unit':'percent','points':[['2026-08-31',100],['2026-09-01',101],['2026-09-30',102],['2026-10-01',999]]}
        def news(key,start,end,refresh=False):
            calls.append(('news',start,end));return {'label':key,'items':[]}
        with patch('tools.macro_helper.server.fetch_series',side_effect=prices),patch('tools.macro_helper.server.news',side_effect=news):
            context,_=analysis.build_context('2026-09','monitor')
        self.assertTrue(all(start==date(2026,9,1) and end==date(2026,9,30) for _,start,end in calls))
        self.assertEqual(len(context['monthly_regimes']),1)
        self.assertNotIn('prior_strategy_results',context);self.assertNotIn('strategy',context)
        self.assertTrue(all(m['last_date']=='2026-09-30' for m in context['monthly_regimes'][0]['metrics']))

    def test_cached_fetch_keeps_original_visited_url_without_claiming_new_visit(self):
        class Response(io.BytesIO):
            def geturl(self):return 'https://data.example.test/final'
        with tempfile.TemporaryDirectory() as folder,patch.object(server,'CACHE_DIR',Path(folder)),patch.object(server,'CACHE',{}),patch.object(server,'urlopen',return_value=Response(b'data')) as get:
            def fetch():server.get_url('https://data.example.test/original');return {'points':[['2026-09-01',100]]}
            first=server.cached('proof',fetch);second=server.cached('proof',fetch)
            get.assert_called_once()
        ledger=Ledger();ledger.fetch(second,'Data')
        self.assertEqual({s['url'] for s in ledger.rows},{'https://data.example.test/original','https://data.example.test/final'})
        self.assertTrue(all(s['access']=='cached_snapshot' for s in ledger.rows))
        self.assertEqual(first['request_log'],second['request_log'])

    def test_failed_visits_remain_listed_but_cannot_support_score(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(server,'CACHE_DIR',Path(folder)),patch.object(server,'CACHE',{}),patch.object(server,'urlopen',side_effect=URLError('fixture failure')):
            result=server.cached('failure',lambda:server.get_url('https://data.example.test/unavailable'))
        ledger=Ledger();ledger.fetch(result,'Unavailable data')
        self.assertEqual(ledger.rows[0]['url'],'https://data.example.test/unavailable')
        self.assertFalse(ledger.rows[0]['available']);self.assertFalse(ledger.rows[0]['request_succeeded'])

    def test_headline_link_is_explicitly_not_an_article_visit(self):
        ledger=Ledger();ledger.headline({'url':'https://news.google.com/articles/test','title':'Fixture headline','publisher':'Fixture publisher','published_at':'2026-09-01T00:00:00Z'})
        self.assertEqual(ledger.rows[0]['access'],'article_not_fetched')
        self.assertEqual(ledger.rows[0]['kind'],'headline_metadata')


if __name__=='__main__':unittest.main()
