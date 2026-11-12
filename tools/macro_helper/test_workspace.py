"""Pair coverage, linked archives, during-month cutoffs and event ledger integrity."""
from datetime import date,datetime,timedelta,timezone
import csv
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from . import analysis,pairs,trades
from .test_analysis import SUMMARY


class WorkspaceChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path_patch=patch.object(analysis,'LOCAL_DIR',Path(self.temp.name));self.path_patch.start()
        self.env_patch=patch.dict('os.environ',{'ANTHROPIC_API_KEY':''});self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop();self.path_patch.stop();self.temp.cleanup()

    def pair_summary(self):
        return {**copy.deepcopy(SUMMARY),'pairs':[{'symbol':s,**copy.deepcopy(SUMMARY)} for s in ('BTCUSDC','ETHUSDC')]}

    def macro(self,perspective='outlook'):
        d={'id':'macro-fixture','report_format':'sentiment-v1','kind':'macro','perspective':perspective,'target_month':'2025-11','generated_at':'2025-10-31T12:00:00+00:00','data_cutoff':'2025-10-31','observation_start':'2025-08-01','reconstruction':True,'model':'fixture','analysis':SUMMARY,'sources':[]}
        with analysis.database() as db:db.execute('INSERT INTO analyses VALUES (?,?,?,?)',(d['id'],d['target_month'],d['generated_at'],json.dumps(d)))
        return d

    def test_monitor_uses_current_month_completed_days_only(self):
        self.assertEqual(analysis.forecast_window('2026-10',date(2026,10,21),'monitor'),(date(2026,10,1),date(2026,10,1),date(2026,10,20)))
        self.assertEqual(analysis.forecast_window('2025-06',date(2026,10,21),'monitor')[-1],date(2025,6,30))
        with self.assertRaises(ValueError):analysis.forecast_window('2026-11',date(2026,10,21),'monitor')

    def test_pair_response_must_cover_every_symbol_exactly_once(self):
        expected=[{'symbol':s,'available':True} for s in ('BTCUSDC','ETHUSDC')]
        valid=self.pair_summary()
        self.assertEqual(len(analysis.validate_pair_output(json.dumps(valid),expected)['pairs']),2)
        for rows in [valid['pairs'][:1], [valid['pairs'][0]]*2, [valid['pairs'][0],{**valid['pairs'][1],'symbol':'DOGEUSDC'}]]:
            with self.assertRaises(ValueError):analysis.validate_pair_output(json.dumps({**valid,'pairs':rows}),expected)

    def test_missing_pair_data_cannot_be_marked_supportive(self):
        expected=[{'symbol':s,'available':s!='ETHUSDC'} for s in ('BTCUSDC','ETHUSDC')]
        response=self.pair_summary()
        with self.assertRaises(ValueError):analysis.validate_pair_output(json.dumps(response),expected)
        for component in response['pairs'][1]['components']:
            component['rating']=None;component['source_ids']=[]
        self.assertEqual(analysis.validate_pair_output(json.dumps(response),expected)['pairs'][1]['sentiment'],'insufficient')

    def test_pair_batch_links_exact_macro_and_reopens_without_paid_call(self):
        macro=self.macro();analysis.save_settings({'provider':'anthropic','api_key':'sk-ant-fixture'})
        context={'kind':'pairs','observation_start':'2025-08-01','data_cutoff':'2025-10-31','reconstruction':True}
        with patch.object(pairs,'build_pair_context',return_value=(context,['Fixture data'])) as build,patch.object(analysis,'ask_model',return_value=(self.pair_summary(),{'input_tokens':10,'output_tokens':20},'claude-haiku-4-5')) as model:
            first=analysis.generate_pairs('2025-11',macro['id'])
            again=analysis.generate_pairs('2025-11',macro['id'])
            self.assertEqual(first['id'],again['id']);self.assertEqual(model.call_count,1)
            unchanged=analysis.generate_pairs('2025-11',macro['id'],True)
            self.assertNotEqual(first['id'],unchanged['id']);self.assertEqual(model.call_count,2)
            self.assertTrue(build.call_args.kwargs['refresh'])
            build.return_value=({**context,'revision':1},['Fixture data'])
            updated=analysis.generate_pairs('2025-11',macro['id'],True)
            self.assertNotEqual(first['id'],updated['id'])
        self.assertEqual(first['macro_analysis_id'],macro['id'])
        self.assertEqual(len(analysis.archive_list('2025-11',kind='pairs')),3)
        self.assertEqual(len(analysis.archive_list('2025-11',kind='macro')),1)
        self.assertAlmostEqual(first['estimated_cost_usd'],.00011)

    def test_macro_refresh_with_unchanged_evidence_generates_new_report(self):
        analysis.save_settings({'provider':'anthropic','api_key':'sk-ant-fixture'})
        context={'observation_start':'2025-08-01','data_cutoff':'2025-10-31','reconstruction':True}
        with patch.object(analysis,'build_context',return_value=(context,['Fixture'])) as build,patch.object(analysis,'ask_model',return_value=(SUMMARY,{},'fixture')) as model:
            original=analysis.generate('2025-11')
            reused=analysis.generate('2025-11',True)
            self.assertEqual(model.call_count,2);self.assertNotEqual(original['id'],reused['id'])
            self.assertNotIn('reused_unchanged',reused)
            self.assertTrue(build.call_args.kwargs['refresh'])
            self.assertEqual(analysis.generate('2025-11')['id'],reused['id'])
            self.assertEqual(model.call_count,2)
        self.assertEqual(len(analysis.archive_list()),2)

    def test_pair_generation_rejects_other_month_or_scope(self):
        macro=self.macro()
        with patch.object(analysis,'ask_model') as model:
            with self.assertRaisesRegex(ValueError,'same month'):analysis.generate_pairs('2025-12',macro['id'])
            with self.assertRaisesRegex(ValueError,'same month'):analysis.generate_pairs('2025-11',macro['id'],perspective='monitor')
            model.assert_not_called()

    def test_legacy_macro_archive_defaults_are_preserved(self):
        d=self.macro()
        del d['kind'];del d['perspective']
        with analysis.database() as db:db.execute('UPDATE analyses SET document=? WHERE id=?',(json.dumps(d),d['id']))
        archive=analysis.archive_list(kind='macro',perspective='outlook')
        self.assertEqual(archive[0]['id'],d['id'])

    def test_pair_context_requires_same_cutoff_before_fetch(self):
        with patch('tools.macro_helper.server.fetch_series') as fetch:
            with self.assertRaisesRegex(ValueError,'same data cutoff'):
                pairs.build_pair_context('2026-10',{'data_cutoff':'2026-10-01'},'monitor')
            fetch.assert_not_called()

    def events(self,*rows,mode='executed'):
        with analysis.database() as db:
            trades.init_tables(db)
            for occurred,symbol,side,event,quantity,price,*pnl in rows:
                key=f'{mode}-{db.total_changes}'
                db.execute('INSERT INTO trade_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(key,mode,symbol,event,side,occurred+'T12:00:00+00:00' if len(occurred)==10 else occurred,quantity,price,(pnl or [None])[0],None,None,'','fixture',key,'{}',occurred))

    def test_trade_fills_fold_into_one_round_trip(self):
        self.events(('2026-10-29T08:54:11+00:00','1000SHIBUSDT','buy','open',150673,0.004833),
                    ('2026-10-29T08:54:11+00:00','1000SHIBUSDT','buy','open',1192468,0.004833),
                    ('2026-10-29T09:02:02+00:00','1000SHIBUSDT','sell','close',1343141,0.004857,32.235384),
                    ('2026-10-29T08:37:34+00:00','TRXUSDT','buy','open',38746,0.33143))
        page=trades.list_trades()
        self.assertEqual(page['total'],2)
        shib=page['trades'][0]
        self.assertEqual((shib['symbol'],shib['side'],shib['fills']),('1000SHIBUSDT','long',3))
        self.assertAlmostEqual(shib['entry_price'],0.004833);self.assertAlmostEqual(shib['exit_price'],0.004857)
        self.assertEqual(shib['reported_pnl'],32.235384)
        self.assertIsNone(page['trades'][1]['closed_at'])

    def test_trade_modes_filters_and_export_keep_original_reported_pnl(self):
        self.events(('2026-10-01','BTCUSDC','long','close',0.01,81000,10))
        self.events(('2026-09-01','ETHUSDC','short','close',0.1,2500,-5),mode='paper')
        self.assertEqual(trades.list_events(mode='paper')['total'],1)
        self.assertEqual(trades.list_events(month='2026-10',symbol='BTCUSDC')['events'][0]['reported_pnl'],10)
        rows=list(csv.DictReader(io.StringIO(trades.export_csv(mode='paper'))))
        self.assertEqual(rows[0]['mode'],'paper');self.assertEqual(float(rows[0]['reported_pnl']),-5)
    def test_trades_filter_by_long_or_short_side(self):
        self.events(('2026-10-01T12:00:00+00:00','BTCUSDT','buy','open',1,80000),
                    ('2026-10-01T13:00:00+00:00','BTCUSDT','sell','close',1,81000,1000),
                    ('2026-10-02T12:00:00+00:00','BTCUSDT','sell','open',1,81000),
                    ('2026-10-02T13:00:00+00:00','BTCUSDT','buy','close',1,80000,1000))
        self.assertEqual(trades.list_trades()['total'],2)
        page=trades.list_trades(side='short')
        self.assertEqual((page['total'],page['trades'][0]['side']),(1,'short'))
        self.assertNotIn('ids',page['trades'][0])
        rows=list(csv.DictReader(io.StringIO(trades.export_csv(side='long'))))
        self.assertEqual(sorted(r['event'] for r in rows),['close','open'])
        self.assertTrue(all(r['occurred_at'].startswith('2026-10-01') for r in rows))
        with self.assertRaisesRegex(ValueError,'side'):trades.list_trades(side='flat')
    def test_trade_events_paginate_newest_first(self):
        self.events(*[(f'2026-10-0{d}','BTCUSDT','long','open',1,80000) for d in range(1,6)])
        page=trades.list_events(limit=2,offset=2)
        self.assertEqual((page['total'],page['offset']),(5,2))
        self.assertEqual([e['occurred_at'][:10] for e in page['events']],['2026-10-03','2026-10-02'])
        with self.assertRaises(ValueError):trades.list_events(offset=-1)


if __name__=='__main__':unittest.main()
