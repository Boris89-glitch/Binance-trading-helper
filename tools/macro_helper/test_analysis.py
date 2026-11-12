"""No paid calls: cutoff integrity, model validation, persistence and API guards."""
from datetime import date, datetime, timezone
import json
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

from . import analysis
from .server import RESULTS, Handler


from .sentiment import COMPONENTS,validate_output
SOURCES=[{'id':'S1','url':'https://example.test/data','title':'Fixture data','kind':'market_data','access':'fetched','available':True}]
RAW_SUMMARY={'paragraphs':['The observed price tone is neutral in this test fixture [S1].','The policy and cross-market tone is mixed in this test fixture [S1].','The source coverage is deliberately synthetic; this is not a market forecast [S1].'],
             'components':[{'key':key,'rating':0,'reason':'Synthetic evidence for this topic.','source_ids':['S1']} for key in COMPONENTS],
             'limitation':'Synthetic fixture only.'}
SUMMARY=validate_output(json.dumps(RAW_SUMMARY),SOURCES)



class AnalysisChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.local=patch.object(analysis,'LOCAL_DIR',Path(self.temp.name))
        self.local.start()
        self.key=patch.dict('os.environ',{'ANTHROPIC_API_KEY':''})
        self.key.start()

    def tearDown(self):
        self.key.stop();self.local.stop();self.temp.cleanup()

    def test_window_for_next_month_ends_at_yesterday(self):
        self.assertEqual(analysis.forecast_window('2026-11',date(2026,10,21)),
                         (date(2026,11,1),date(2026,10,1),date(2026,10,20)))

    def test_historical_window_ends_before_target(self):
        self.assertEqual(analysis.forecast_window('2025-02',date(2026,10,21)),
                         (date(2025,2,1),date(2025,1,1),date(2025,1,31)))
        with self.assertRaises(ValueError):analysis.forecast_window('2026-12',date(2026,10,21))
        with self.assertRaises(ValueError):analysis.forecast_window('2025-1',date(2026,10,21))

    def test_historical_strategy_context_has_no_future_results(self):
        rows=analysis.past_results(RESULTS,date(2025,10,31))
        self.assertEqual(len(rows),10)
        self.assertTrue(all(r['month']<'2025-11' for r in rows))
        self.assertNotIn('2026-01',[r['month'] for r in rows])

    def test_partial_result_not_available_before_its_cutoff(self):
        self.assertNotIn('2026-10',[r['month'] for r in analysis.past_results(RESULTS,date(2026,10,16))])
        rows=analysis.past_results(RESULTS,date(2026,10,17))
        self.assertTrue(rows[-1]['partial'])

    def test_key_status_never_returns_key(self):
        key='sk-ant-unit-test-secret'
        result=analysis.save_settings({'provider':'anthropic','api_key':key})
        self.assertTrue(result['configured'])
        self.assertNotIn(key,json.dumps(result))
        self.assertNotIn('api_key',result)
        self.assertEqual(analysis.settings()['api_key'],key)

    def test_invalid_model_output_cannot_be_archived(self):
        for text in ['not json',json.dumps({**RAW_SUMMARY,'paragraphs':['one','two']}),json.dumps({**RAW_SUMMARY,'components':[]})]:
            with self.assertRaises(ValueError):analysis.validate_output(text,SOURCES)
        self.assertEqual(analysis.validate_output(json.dumps(RAW_SUMMARY),SOURCES),SUMMARY)

    def fixture_context(self,target,**kwargs):
        return {'data_cutoff':'2025-10-31','observation_start':'2025-08-01','reconstruction':True,'revision':getattr(self,'revision',0)},['Fixture provider']

    def test_archive_reopen_never_calls_model_and_updates_keep_versions(self):
        analysis.save_settings({'provider':'anthropic','api_key':'sk-ant-unit-test'})
        with patch.object(analysis,'build_context',side_effect=self.fixture_context),patch.object(analysis,'ask_model',return_value=(SUMMARY,{'input_tokens':10,'output_tokens':20},'fixture-model')) as model:
            first=analysis.generate('2025-11')
            reopened=analysis.generate('2025-11')
            self.assertEqual(first,reopened)
            self.assertEqual(model.call_count,1)
            self.revision=1
            second=analysis.generate('2025-11',regenerate=True)
            self.assertNotEqual(first['id'],second['id'])
            self.assertEqual(model.call_count,2)
        self.assertEqual(len(analysis.archive_list()),2)
        self.assertEqual(analysis.archive_get(first['id']),first)
        self.assertNotIn('sk-ant',json.dumps(first))
        # A new connection still reads both reports: this is not process memory.
        self.assertEqual(len(analysis.archive_list('2025-11')),2)

    def test_provider_failure_preserves_archive(self):
        analysis.save_settings({'provider':'anthropic','api_key':'sk-ant-unit-test'})
        with patch.object(analysis,'build_context',side_effect=self.fixture_context),patch.object(analysis,'ask_model',side_effect=ValueError('Provider unavailable')):
            with self.assertRaises(ValueError):analysis.generate('2025-11')
        self.assertEqual(analysis.archive_list(),[])

    def test_missing_key_does_not_fetch_or_call_model(self):
        (analysis.LOCAL_DIR/'settings.json').write_text(json.dumps({'provider':'anthropic'}),encoding='utf-8')
        with patch.object(analysis,'build_context') as context,patch.object(analysis,'ask_model') as model:
            with self.assertRaisesRegex(ValueError,'API key'):analysis.generate('2025-11')
            context.assert_not_called();model.assert_not_called()

    def test_duplicate_generation_is_blocked(self):
        analysis.GENERATION_LOCK.acquire()
        try:
            with self.assertRaisesRegex(ValueError,'already generating'):analysis.generate('2025-11')
        finally:analysis.GENERATION_LOCK.release()

    def test_anthropic_request_and_response_without_paid_call(self):
        response={'content':[{'type':'text','text':json.dumps(SUMMARY)}], 'stop_reason':'end_turn', 'usage':{'input_tokens':50,'output_tokens':70},'model':'fixture-model'}
        with patch.object(analysis,'urlopen',return_value=io.BytesIO(json.dumps(response).encode())) as post:
            summary,usage,model=analysis.ask_model({'target_month':'2026-11','source_ledger':SOURCES},{'api_key':'sk-ant-test','model':analysis.DEFAULT_MODEL})
        self.assertEqual(summary,SUMMARY)
        self.assertEqual(usage['input_tokens'],50)
        request=post.call_args.args[0]
        self.assertEqual(request.full_url,'https://api.anthropic.com/v1/messages')
        payload=json.loads(request.data)
        self.assertEqual(payload['max_tokens'],3500)
        self.assertNotIn('sk-ant-test',request.data.decode())

    def test_provider_errors_do_not_expose_credentials(self):
        error=HTTPError('https://api.anthropic.com/v1/messages',401,'secret raw provider detail',{},None)
        with patch.object(analysis,'urlopen',side_effect=error):
            with self.assertRaises(ValueError) as caught:
                analysis.ask_model({}, {'api_key':'sk-ant-test-secret','model':analysis.DEFAULT_MODEL})
        self.assertIn('API key was rejected',str(caught.exception))
        self.assertNotIn('secret',str(caught.exception))

    def test_real_context_fetches_only_pre_target_dates(self):
        from .server import SERIES
        calls=[]
        def prices(key,start,end,refresh=False):
            calls.append((start,end))
            return {'key':key,'label':SERIES[key][0],'unit':SERIES[key][3],'provider':'fixture','points':[['2025-07-31',100],['2025-08-31',101],['2025-09-30',102],['2025-10-31',103]]}
        def news(key,start,end,refresh=False):
            calls.append((start,end));return {'label':key,'items':[]}
        with patch('tools.macro_helper.server.fetch_series',side_effect=prices),patch('tools.macro_helper.server.news',side_effect=news):
            context,sources=analysis.build_context('2025-11')
        self.assertTrue(all(end<date(2025,11,1) for start,end in calls))
        self.assertNotIn('prior_strategy_results',context)
        self.assertNotIn('strategy',context)
        self.assertEqual(len(context['monthly_regimes']),1)
        self.assertEqual(context['observation_start'],'2025-10-01')
        self.assertEqual(context['monthly_regimes'][-1]['end'],'2025-10-31')

    def test_http_denies_foreign_origin_and_missing_key_without_exposure(self):
        (analysis.LOCAL_DIR/'settings.json').write_text(json.dumps({'provider':'anthropic'}),encoding='utf-8')
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            request=Request(base+'/api/analysis',data=b'{"target_month":"2025-11"}',headers={'Content-Type':'application/json','Origin':'https://evil.example'})
            with self.assertRaises(HTTPError) as caught:urlopen(request)
            self.assertEqual(caught.exception.code,403)
            request=Request(base+'/api/analysis',data=b'{"target_month":"2025-11"}',headers={'Content-Type':'application/json'})
            with self.assertRaises(HTTPError) as caught:urlopen(request)
            self.assertEqual(caught.exception.code,400)
            with urlopen(base+'/api/ai/status') as response:
                self.assertFalse(json.load(response)['configured'])
        finally:
            server.shutdown();server.server_close();worker.join()


if __name__=='__main__':unittest.main()
