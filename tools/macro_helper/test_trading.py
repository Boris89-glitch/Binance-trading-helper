"""Execution safeguards with an in-memory exchange; never sends real orders."""
import copy
import io
import hashlib
import hmac
from http.server import ThreadingHTTPServer
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import time
import threading
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
from . import trading as t
from .server import Handler


INFO={'symbol':'BTCUSDT','status':'TRADING','contractType':'PERPETUAL','quoteAsset':'USDT','marginAsset':'USDT',
      'filters':[{'filterType':'LOT_SIZE','stepSize':'0.001','minQty':'0.001','maxQty':'100'},
                 {'filterType':'MARKET_LOT_SIZE','stepSize':'0.001','minQty':'0.001','maxQty':'50'},
                 {'filterType':'PRICE_FILTER','tickSize':'0.10'}, {'filterType':'MIN_NOTIONAL','notional':'5'}]}


INFO_USDC={**INFO,'symbol':'BTCUSDC','quoteAsset':'USDC','marginAsset':'USDC'}


class FakeClient:
    offset=0
    def __init__(self):
        self.calls=[];self.amount=Decimal(0);self.entry=Decimal(1000);self.orders=[];self.hedge=False
        self.fail_protection=False;self.timeout=False;self.order_results={};self.fixed_stop=False;self.terse=False;self.reject_maker=False;self.spread=False
    def position(self):
        return {'symbol':'BTCUSDT','positionAmt':str(self.amount),'positionSide':'BOTH','entryPrice':str(self.entry),'markPrice':'1000','unRealizedProfit':'0'}
    def request(self,method,path,params=None,signed=True):
        params=params or {};self.calls.append((method,path,copy.deepcopy(params)))
        if path.endswith('/positionSide/dual'):return {'dualSidePosition':self.hedge}
        if path.endswith('/exchangeInfo'):return {'symbols':[INFO,INFO_USDC]}
        if path.endswith('/positionRisk'):return [self.position()] if self.amount else []
        if path.endswith('/bookTicker'):return {'bidPrice':'999.9','askPrice':'1000'} if self.spread else {'bidPrice':'1000','askPrice':'1000'}
        if path.endswith('/balance'):return [{'asset':a,'balance':'10000','availableBalance':'10000'} for a in ('USDT','USDC')]
        if path.endswith('/openAlgoOrders'):return copy.deepcopy(self.orders)
        if path.endswith('/openOrders'):return [o for o in self.order_results.values() if o.get('type')=='LIMIT' and o['status']=='NEW']
        if path.endswith('/klines'):return [[i,0,'1010','990','1000',0,i+1] for i in range(30)]
        if path.endswith('/leverage'):return {'leverage':params['leverage']}
        if path.endswith('/allOrders'):return [{**o,'clientOrderId':cid} for cid,o in self.order_results.items() if 'orderId' in o]
        if path.endswith('/allAlgoOrders'):return copy.deepcopy(self.orders)
        if path.endswith('/userTrades'):return [{'id':o['orderId'],'orderId':o['orderId'],'side':o['side'],'qty':o['executedQty'],'price':'1000','realizedPnl':'0','commission':'0.1','commissionAsset':'USDT','time':1700000000000} for o in self.order_results.values() if 'orderId' in o]
        if path.endswith('/leverageBracket'):return [{'symbol':params['symbol'],'brackets':[{'bracket':1,'initialLeverage':125,'notionalCap':500},{'bracket':2,'initialLeverage':100,'notionalCap':10000},{'bracket':3,'initialLeverage':50,'notionalCap':100000}]}]
        if path.endswith('/algoOrder'):
            if method=='GET':
                for order in self.orders:
                    if order['clientAlgoId']==params['clientAlgoId']:return order
                raise t.ExchangeError('No algo',-2013)
            if method=='DELETE':self.orders=[o for o in self.orders if o['algoId']!=params['algoId']];return {}
            if self.fail_protection:raise t.ExchangeError('Protection rejected',-2021)
            if any(o['clientAlgoId']==params['clientAlgoId'] for o in self.orders):raise t.ExchangeError('Duplicate',-4116)
            order={**params,'orderType':params['type'],'algoId':len(self.orders)+1};self.orders.append(order);return order
        if path.endswith('/order'):
            if method=='GET':
                if params['origClientOrderId'] in self.order_results:return self.order_results[params['origClientOrderId']]
                raise t.ExchangeError('No order',-2013)
            if method=='DELETE':
                order=self.order_results[params['origClientOrderId']]
                if order['status']!='NEW':raise t.ExchangeError('Unknown order',-2011)
                order['status']='CANCELED';return order
            if self.timeout:raise t.ExchangeError('Uncertain timeout',uncertain=True)
            if params['type']=='LIMIT':
                if self.reject_maker:raise t.ExchangeError('Post only rejected',-5022)
                order={'status':'NEW','type':'LIMIT','executedQty':'0','avgPrice':'0','orderId':len(self.order_results)+1,'side':params['side'],'price':params['price'],'origQty':params['quantity'],'reduceOnly':params.get('reduceOnly','false')}
                self.order_results[params['newClientOrderId']]=order;return order
            qty=Decimal(params['quantity']);direction=1 if params['side']=='BUY' else -1
            if params.get('reduceOnly')=='true':
                if self.amount*direction>=0:raise t.ExchangeError('Reduce only rejected')
                qty=min(qty,abs(self.amount))
            self.amount+=direction*qty
            order={'status':'FILLED','executedQty':str(qty),'avgPrice':'1000','orderId':len(self.order_results)+1,'side':params['side']}
            self.order_results[params['newClientOrderId']]=order
            return {k:v for k,v in order.items() if k!='avgPrice'} if self.terse else order
        raise AssertionError((method,path,params))


class TradingChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.local=patch.object(t.analysis,'LOCAL_DIR',Path(self.temp.name));self.local.start()
        self.client=FakeClient();self.client_patch=patch.object(t,'Client',return_value=self.client);self.client_patch.start()
        t.INFO_CACHE.clear();t.CLOCK.clear();self.sync=patch.object(t,'start_sync');self.sync.start()
        self.counter=0
    def tearDown(self):
        self.sync.stop();self.client_patch.stop();self.local.stop();self.temp.cleanup()
    def payload(self,action='buy',**kwargs):
        self.counter+=1
        return {**t.DEFAULT,'action':action,'request_id':f'{self.counter:x}'.ljust(32,'0'),**kwargs}
    def open_position(self,amount='1',stop='950',full=True):
        self.client.amount=Decimal(amount)
        self.client.orders=[{'symbol':'BTCUSDT','side':'SELL' if Decimal(amount)>0 else 'BUY','orderType':'STOP_MARKET','triggerPrice':stop,'closePosition':full,'reduceOnly':not full,'quantity':str(abs(Decimal(amount))),'positionSide':'BOTH','workingType':'MARK_PRICE','clientAlgoId':'external-stop','algoId':999}]
    def market_calls(self):return [p for m,path,p in self.client.calls if m=='POST' and path=='/fapi/v1/order']
    def test_default_config_and_roundtrip(self):
        result=t.save_config({'name':'My setup','risk':25,'adjust_risk':12})
        self.assertEqual(result['configs'][0]['risk'],25)
        self.assertEqual(result['configs'][0]['stop_atr'],2.5)
        self.assertEqual(t.configs()['configs'][0]['adjust_risk'],12)
        self.assertEqual(t.save_config({'name':'My setup','delete':True})['configs'],[])
    def test_retired_timeframe_is_rejected_and_old_configs_fall_back(self):
        with self.assertRaisesRegex(ValueError,'ATR timeframe'):t.config({'timeframe':'5m'})
        t.save_config({'name':'Old'})
        with t.database() as db:db.execute('UPDATE configs SET body=?',(json.dumps({**t.DEFAULT,'timeframe':'5m'}),))
        self.assertEqual(t.configs()['configs'][0]['timeframe'],'30m')
    def test_bad_numbers_and_duplicate_keys_rejected(self):
        for n in ('nan','inf',-1,0):
            with self.assertRaises(ValueError):t.config({'risk':n})
        with self.assertRaises(ValueError):t.config({'keys':dict.fromkeys(t.DEFAULT['keys'],'B')})
    def test_quantity_rounds_down_and_never_raises_risk_to_minimum(self):
        self.assertEqual(t.quantity(INFO,Decimal('.1239'),Decimal(1000)),Decimal('.123'))
        with self.assertRaises(ValueError):t.quantity(INFO,Decimal('.0009'),Decimal(1000))
        with self.assertRaises(ValueError):t.quantity(INFO,Decimal(51),Decimal(1000))
    def test_incomplete_candle_excluded(self):
        candles=[[i,0,'1010','990','1000',0,i+1] for i in range(16)]
        candles.append([100,0,'99999','1','99999',0,99999])
        self.assertEqual(t.atr(candles,50),Decimal(20))
    def test_entry_sizing_and_brackets(self):
        result=t.execute(self.payload())
        self.assertEqual(self.client.amount,Decimal(1))
        self.assertEqual(Decimal(result['stop']),950)
        self.assertEqual(Decimal(result['target']),1080)
        self.assertTrue(all(o['closePosition']=='true' for o in self.client.orders))
    def test_entry_protected_when_submit_response_lacks_fill_price(self):
        self.client.terse=True
        result=t.execute(self.payload())
        self.assertEqual(Decimal(result['stop']),950);self.assertEqual(len(self.client.orders),2)
    def test_desk_fills_are_archived_once(self):
        with patch.object(t.analysis,'LOCAL_DIR',Path(self.temp.name)):
            t.execute(self.payload());t.execute(self.payload('reduce',position_side='LONG',adjust_risk=10))
            t.sync_fills(self.client);t.sync_fills(self.client)
            events=t.trades.list_events()['events']
        self.assertEqual(sorted((e['event'],e['side']) for e in events),[('open','buy'),('partial_close','sell')])
    def manual_close(self,pnl):
        self.client.amount=Decimal(0);self.client.orders=[]
        self.client.order_results['web_manual']={'status':'FILLED','executedQty':'1','orderId':99,'side':'SELL','reduceOnly':True}
        request=self.client.request
        def patched(method,path,params=None,signed=True):
            result=request(method,path,params,signed)
            if path.endswith('/userTrades'):
                for fill in result:
                    if fill['orderId']==99:fill.update(realizedPnl=pnl,time=fill['time']+1000)
            return result
        self.client.request=patched
    def test_manual_close_ends_the_archived_trade(self):
        for pnl in ('12.5','0'):
            with self.subTest(pnl=pnl):
                self.tearDown();self.setUp()
                t.execute(self.payload());self.manual_close(pnl)
                self.assertFalse(t.sync_fills(self.client))
                trip=t.trades.list_trades()['trades'][0]
                self.assertEqual(trip['side'],'long');self.assertTrue(trip['closed_at']);self.assertEqual(trip['reported_pnl'],float(pnl))
    def test_outside_fill_without_pnl_adds_to_the_trade(self):
        t.execute(self.payload())
        self.client.order_results['web_add']={'status':'FILLED','executedQty':'1','orderId':99,'side':'BUY','reduceOnly':False}
        self.assertTrue(t.sync_fills(self.client))
        self.assertEqual(t.trades.list_trades()['trades'][0]['closed_at'],None)
    def test_watch_survives_flat_account_while_limit_works(self):
        self.client.spread=True
        with patch.object(t,'ensure_watcher'):t.execute(self.payload('limit_buy'))
        self.assertTrue(t.sync_fills(self.client))
        with t.database() as db:self.assertEqual(db.execute('SELECT active FROM fill_sync').fetchone()[0],1)
    def test_order_started_during_sync_keeps_the_watch(self):
        t.execute(self.payload());self.manual_close('1')
        positions=t.positions
        def late(client,symbol=None):
            with t.database() as db:db.execute("INSERT INTO operations VALUES ('late','BTCUSDT','buy','done','{}','{}',?)",(time.time()+1,))
            return positions(client,symbol)
        with patch.object(t,'positions',late):t.sync_fills(self.client)
        with t.database() as db:self.assertEqual(db.execute('SELECT active FROM fill_sync').fetchone()[0],1)
    def test_outside_fills_backfill_once_from_the_first_operation(self):
        t.execute(self.payload());self.manual_close('1')
        with t.database() as db:db.execute('UPDATE fill_sync SET active=0,since=?',(int(time.time()*1000)+5000,))
        t.sync_fills(self.client)
        self.assertTrue(t.trades.list_trades()['trades'][0]['closed_at'])
        with t.database() as db:
            db.execute('UPDATE fill_sync SET active=0');self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
        t.sync_fills(self.client)
        with t.database() as db:self.assertEqual(db.execute('SELECT active FROM fill_sync').fetchone()[0],0)
    def test_one_failing_contract_does_not_block_the_rest(self):
        t.execute(self.payload());t.watch('ETHUSDT',0);self.client.amount=Decimal(0);self.client.orders=[]
        request=self.client.request
        def flaky(method,path,params=None,signed=True):
            if (params or {}).get('symbol')=='ETHUSDT':raise t.ExchangeError('Timeout')
            return request(method,path,params,signed)
        self.client.request=flaky
        self.assertTrue(t.sync_fills(self.client))
        self.assertTrue(t.trades.list_events()['events'])
    def test_sync_loop_stops_when_nothing_is_watched(self):
        with patch.object(t,'sync_fills',side_effect=[ValueError('offline'),True,False]) as sync,patch.object(t.time,'sleep'):t.sync_loop()
        self.assertEqual(sync.call_count,3)
    def protect(self,action,**kwargs):
        return t.execute(self.payload(action,position_side='LONG' if self.client.amount>0 else 'SHORT',**kwargs))
    def test_stop_and_target_are_set_individually_from_atr(self):
        self.client.amount=Decimal(1)
        self.assertEqual(self.protect('stop',stop_atr=2.5)['price'],'950.0')
        self.assertEqual([(o['type'],o['side'],o['triggerPrice'],o['closePosition']) for o in self.client.orders],[('STOP_MARKET','SELL','950.0','true')])
        row=t.snapshot()['positions'][0];self.assertEqual((row['stop'],row['target']),('950.0',None))
        self.assertEqual(self.protect('target',target_atr=4)['price'],'1080.0')
        self.assertEqual(t.snapshot()['positions'][0]['target'],'1080.0')
    def test_short_protection_mirrors_the_entry(self):
        self.client.amount=Decimal(-1)
        self.assertEqual(self.protect('stop',stop_atr=2.5)['price'],'1050.0')
        self.assertEqual(self.protect('target',target_atr=4)['price'],'920.0')
        self.assertEqual({o['side'] for o in self.client.orders},{'BUY'})
    def test_timeframe_comes_from_the_request(self):
        self.client.amount=Decimal(1);self.protect('stop',timeframe='4h')
        self.assertIn('4h',[p.get('interval') for m,path,p in self.client.calls if path.endswith('/klines')])
    def test_existing_protection_is_not_replaced(self):
        self.client.amount=Decimal(1);self.protect('stop')
        with self.assertRaisesRegex(ValueError,'already has a stop'):self.protect('stop',stop_atr=3)
        self.assertEqual(len(self.client.orders),1)
    def test_stop_past_the_mark_is_measured_from_the_mark(self):
        self.client.amount=Decimal(1);self.client.entry=Decimal(1100)
        result=self.protect('stop',stop_atr=2.5)
        self.assertEqual(result['price'],'950.0');self.assertIn('mark price',result['message'])
        self.assertEqual([o['triggerPrice'] for o in self.client.orders],['950.0'])
    def test_target_past_the_mark_is_rejected(self):
        self.client.amount=Decimal(1);self.client.entry=Decimal(900)
        with self.assertRaisesRegex(ValueError,'past the mark'):self.protect('target',target_atr=4)
        self.assertEqual(self.client.orders,[])
    def test_protection_checks_the_selected_direction(self):
        self.client.amount=Decimal(1)
        with self.assertRaisesRegex(ValueError,'direction'):t.execute(self.payload('stop',position_side='SHORT'))
        self.client.amount=Decimal(0)
        with self.assertRaisesRegex(ValueError,'no longer exists'):t.execute(self.payload('target',position_side='LONG'))
    def test_uncertain_protection_blocks_until_reconciled(self):
        self.client.amount=Decimal(1)
        with patch.object(t,'bracket',side_effect=t.ExchangeError('Timeout',uncertain=True)):
            with self.assertRaises(ValueError):self.protect('stop')
        self.assertEqual(t.snapshot()['pending'][0]['action'],'stop')
        self.assertIn('not active',t.reconcile()['results'][0]['message'])
        self.assertEqual(t.snapshot()['pending'],[])
    def test_triggered_protection_is_archived_as_a_close(self):
        self.client.amount=Decimal(1);self.protect('stop')
        self.assertTrue(t.sync_fills(self.client))
        with t.database() as db:self.assertEqual(db.execute("SELECT count(*) FROM operations WHERE action='stop' AND state='done'").fetchone()[0],1)
    def test_entry_leverage_follows_brackets(self):
        t.execute(self.payload())
        self.assertEqual([p['leverage'] for m,path,p in self.client.calls if path=='/fapi/v1/leverage'],[100])
        self.assertEqual(t.select_leverage(self.client,'BTCUSDT',Decimal(400)),125)
        self.assertEqual(t.select_leverage(self.client,'BTCUSDT',Decimal(10**7)),50)
    def test_short_entry_protection_direction(self):
        result=t.execute(self.payload('sell'))
        self.assertEqual(Decimal(result['stop']),1050);self.assertEqual(Decimal(result['target']),920)
        self.assertTrue(all(o['side']=='BUY' for o in self.client.orders))
    def test_replayed_request_does_not_duplicate_order(self):
        data=self.payload();one=t.execute(data);two=t.execute(data)
        self.assertEqual(one,two);self.assertEqual(len(self.market_calls()),1)
    def test_market_entry_rejects_usdc_contract(self):
        with patch.dict(INFO,{'symbol':'BTCUSDC','quoteAsset':'USDC','marginAsset':'USDC'}):
            with self.assertRaisesRegex(ValueError,'not available'):t.execute(self.payload(symbol='BTCUSDC'))
        self.assertEqual(self.market_calls(),[])
    def test_existing_position_rejects_new_entry(self):
        self.open_position()
        with self.assertRaises(ValueError):t.execute(self.payload())
        self.assertEqual(self.market_calls(),[])
    def test_topup_uses_market_to_stop_distance(self):
        self.open_position();self.client.entry=Decimal(980)
        result=t.execute(self.payload('topup',position_side='LONG',adjust_risk=10))
        self.assertEqual(self.client.amount,Decimal('1.2'))
        self.assertEqual(Decimal(result['risk_change_estimate']),Decimal(10))
    def test_reduction_uses_entry_to_stop_distance(self):
        self.open_position();self.client.entry=Decimal(980)
        result=t.execute(self.payload('reduce',position_side='LONG',adjust_risk=10))
        self.assertEqual(self.client.amount,Decimal('.667'))
        self.assertEqual(self.market_calls()[0]['reduceOnly'],'true')
        self.assertLessEqual(Decimal(result['risk_change_estimate']),10)
    def test_short_risk_adjustments(self):
        self.open_position('-1','1050')
        t.execute(self.payload('topup',position_side='SHORT',adjust_risk=10));self.assertEqual(self.client.amount,Decimal('-1.2'))
        t.execute(self.payload('reduce',position_side='SHORT',adjust_risk=10));self.assertEqual(self.client.amount,Decimal('-1'))
    def test_reduction_cannot_flip_or_silently_close(self):
        self.open_position()
        with self.assertRaisesRegex(ValueError,'close it on Binance'):t.execute(self.payload('reduce',position_side='LONG',adjust_risk=60))
        self.assertEqual(self.market_calls(),[])
    def test_desk_has_no_market_close(self):
        self.client.amount=Decimal(1)
        with self.assertRaisesRegex(ValueError,'Unknown trading action'):t.execute(self.payload('close',position_side='LONG'))
        self.assertEqual(self.market_calls(),[])
    def test_topup_and_reduce_without_stop_size_by_atr(self):
        self.client.amount=Decimal(1)
        result=t.execute(self.payload('topup',position_side='LONG',adjust_risk=50))
        # ATR 20 x stop 2.5 = 50 distance, so $50 risk adds 1 contract and places no protection.
        self.assertEqual(self.market_calls()[0]['quantity'],'1');self.assertEqual(self.client.amount,2)
        self.assertEqual(self.client.orders,[]);self.assertEqual(Decimal(result['risk_change_estimate']),50)
        t.execute(self.payload('reduce',position_side='LONG',adjust_risk=50))
        self.assertEqual(self.client.amount,1)
    def test_fixed_quantity_stop_promoted_before_topup(self):
        self.open_position(full=False)
        t.execute(self.payload('topup',position_side='LONG'))
        paths=[path for method,path,p in self.client.calls if method=='POST']
        self.assertLess(paths.index('/fapi/v1/algoOrder'),paths.index('/fapi/v1/order'))
        self.assertEqual(len(self.client.orders),2)
    def test_profit_lock_cannot_be_sized_as_loss_risk(self):
        self.open_position(stop='1010')
        with self.assertRaisesRegex(ValueError,'profit'):t.execute(self.payload('reduce',position_side='LONG'))
    def test_changed_direction_rejected(self):
        self.open_position('-1','1050')
        with self.assertRaisesRegex(ValueError,'direction'):t.execute(self.payload('reduce',position_side='LONG'))
    def test_hedge_mode_never_mutated(self):
        self.client.hedge=True
        with self.assertRaisesRegex(ValueError,'one-way'):t.execute(self.payload())
        self.assertFalse(any(m=='POST' for m,p,d in self.client.calls))
    def test_protection_failure_emergency_closes(self):
        self.client.fail_protection=True
        result=t.execute(self.payload())
        self.assertTrue(result['warning']);self.assertEqual(self.client.amount,0)
        self.assertEqual(self.market_calls()[-1]['reduceOnly'],'true')
    def test_uncertain_market_order_is_not_retried(self):
        self.client.timeout=True
        with self.assertRaisesRegex(ValueError,'unknown'):t.execute(self.payload())
        self.assertEqual(len(self.market_calls()),1)
        with self.assertRaisesRegex(ValueError,'Reconcile'):t.execute(self.payload())
        self.assertEqual(len(self.market_calls()),1)
    def test_reconciliation_finishes_protection_without_new_entry(self):
        self.client.timeout=True;data=self.payload()
        with self.assertRaises(ValueError):t.execute(data)
        self.client.timeout=False;self.client.amount=Decimal(1)
        self.client.order_results[t.PREFIX+data['request_id'][:28]]={'status':'FILLED','executedQty':'1','avgPrice':'1000'}
        t.reconcile()
        self.assertEqual(len(self.market_calls()),1);self.assertEqual(len(self.client.orders),2)
        self.assertEqual(t.snapshot()['pending'],[])
    def test_cleanup_keeps_other_app_orders(self):
        self.open_position();self.client.orders.append({**self.client.orders[0],'clientAlgoId':'rh-test','algoId':1000})
        t.cleanup(self.client,'BTCUSDT')
        self.assertEqual([o['clientAlgoId'] for o in self.client.orders],['external-stop'])

    def test_reconcile_existing_stop_does_not_emergency_close(self):
        data=self.payload();t.execute(data)
        with t.database() as db:db.execute("UPDATE operations SET state='unknown' WHERE id=?",(data['request_id'],))
        t.reconcile()
        self.assertEqual(len(self.market_calls()),1);self.assertEqual(self.client.amount,1)

    def test_missing_order_stays_blocked_until_receive_window_passes(self):
        self.client.timeout=True;data=self.payload()
        with self.assertRaises(ValueError):t.execute(data)
        t.reconcile();self.assertEqual(len(t.snapshot()['pending']),1)
        with t.database() as db:db.execute('UPDATE operations SET created=?',(time.time()-120,))
        t.reconcile();self.assertEqual(t.snapshot()['pending'],[])
        self.assertEqual(len(self.market_calls()),1)

    def test_http_trading_requires_same_origin_and_json(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            for origin in (None,'https://foreign.example'):
                headers={'Content-Type':'application/json'}
                if origin:headers['Origin']=origin
                with self.assertRaises(HTTPError) as caught:urlopen(Request(base+'/api/trading/order',data=b'{}',headers=headers))
                self.assertEqual(caught.exception.code,403)
            with urlopen(Request(base+'/api/trading/configs',data=b'{"name":"HTTP setup"}',headers={'Content-Type':'application/json','Origin':base})) as response:
                self.assertEqual(json.load(response)['configs'][0]['name'],'HTTP setup')
            self.assertEqual(self.market_calls(),[])
        finally:server.shutdown();server.server_close();worker.join()


class TransportChecks(unittest.TestCase):
    def client(self):
        client=t.Client.__new__(t.Client)
        client.key='test-public-key';client.secret='test-secret';client.offset=0;client.opener=Mock()
        return client
    def test_signed_requests_only_target_testnet(self):
        client=self.client();client.opener.open.return_value=io.BytesIO(b'{}')
        client.request('POST','/fapi/v1/order',{'symbol':'BTCUSDT'})
        req=client.opener.open.call_args.args[0]
        self.assertEqual(urlparse(req.full_url).netloc,'demo-fapi.binance.com')
        query,signature=urlparse(req.full_url).query.rsplit('&signature=',1)
        self.assertEqual(signature,hmac.new(b'test-secret',query.encode(),hashlib.sha256).hexdigest())
        self.assertNotIn('test-secret',req.full_url)
    def test_transport_timeout_never_retries(self):
        client=self.client();client.opener.open.side_effect=URLError('secret URL')
        with self.assertRaises(t.ExchangeError) as caught:client.request('POST','/fapi/v1/order')
        self.assertTrue(caught.exception.uncertain);self.assertNotIn('secret URL',str(caught.exception));self.assertEqual(client.opener.open.call_count,1)
    def test_clock_drift_resyncs_and_resends_once(self):
        client=self.client();drift=HTTPError('u',400,'bad',{},io.BytesIO(b'{"code":-1021,"msg":"outside recvWindow"}'))
        server=int(time.time()*1000)+3000
        client.opener.open.side_effect=[drift,io.BytesIO(json.dumps({'serverTime':server}).encode()),io.BytesIO(b'{"ok":1}')]
        self.assertEqual(client.request('POST','/fapi/v1/order',{'symbol':'BTCUSDT'}),{'ok':1})
        self.assertGreater(client.offset,2000);self.assertEqual(client.opener.open.call_count,3)
    def test_production_credentials_are_not_fallbacks(self):
        with patch('dotenv.dotenv_values',return_value={}),patch.dict('os.environ',{'BINANCE_TEST_FUTURES_API_KEY':'','BINANCE_TEST_FUTURES_SECRET_KEY':'','BINANCE_FUTURES_API_KEY':'prod','BINANCE_FUTURES_SECRET_KEY':'prod'}):
            with self.assertRaisesRegex(ValueError,'testnet credentials'):t.credentials()


class LimitChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.local=patch.object(t.analysis,'LOCAL_DIR',Path(self.temp.name));self.local.start()
        self.client=FakeClient();self.client.spread=True;t.INFO_CACHE.clear();self.patches=[patch.object(t,'Client',return_value=self.client),patch.object(t,'ensure_watcher'),patch.object(t,'start_sync')]
        for item in self.patches:item.start()
        self.counter=0
    def tearDown(self):
        for item in self.patches:item.stop()
        self.local.stop();self.temp.cleanup()
    def payload(self,action='limit_buy',**kwargs):
        self.counter+=1
        return {**t.DEFAULT,'action':action,'request_id':f'{self.counter:x}'.ljust(32,'a'),**kwargs}
    def posts(self,path):return [p for m,x,p in self.client.calls if m=='POST' and x==path]
    def fill(self,qty='0.5',status='FILLED'):
        order=next(iter(self.client.order_results.values()));order.update(status=status,executedQty=qty,avgPrice=order['price'])
        self.client.amount=Decimal(qty)*(1 if order['side']=='BUY' else -1)
    def test_limit_rests_on_usdt_without_stop_or_target(self):
        result=t.execute(self.payload())
        order=self.posts('/fapi/v1/order')
        self.assertEqual(self.posts('/fapi/v1/algoOrder'),[])
        self.assertEqual((order[0]['symbol'],order[0]['type'],order[0]['timeInForce'],order[0]['price']),('BTCUSDT','LIMIT','GTX','999.9'))
        self.assertIn('no stop or target',result['message'])
        self.assertEqual([l['symbol'] for l in t.snapshot()['limits']],['BTCUSDT'])
    def test_fill_finishes_without_protection(self):
        t.execute(self.payload());self.fill()
        notices=t.check_limits(self.client)
        self.assertEqual(self.posts('/fapi/v1/algoOrder'),[])
        self.assertIn('filled',notices[0]);self.assertEqual(t.snapshot()['limits'],[])
    def test_partial_fill_keeps_working(self):
        t.execute(self.payload());self.fill('0.2','PARTIALLY_FILLED')
        t.check_limits(self.client);t.check_limits(self.client)
        self.assertEqual(self.posts('/fapi/v1/algoOrder'),[])
        self.assertEqual(t.snapshot()['limits'][0]['filled'],'0.2')
    def test_cancel_without_fill(self):
        t.execute(self.payload());limit=t.snapshot()['limits'][0]
        result=t.cancel_limit({'limit_id':limit['id']})
        self.assertEqual(self.client.orders,[]);self.assertIn('without a fill',result['message'])
    def test_up_to_three_limits_stack_on_unprotected_position(self):
        self.client.amount=Decimal(1)
        for price in ('990','980','970'):t.execute(self.payload(limit_price=price))
        self.assertEqual(len(self.posts('/fapi/v1/order')),3)
        self.assertEqual([p for m,x,p in self.client.calls if x=='/fapi/v1/leverage'],[])
        with self.assertRaisesRegex(ValueError,'3 working limits'):t.execute(self.payload(limit_price='960'))
    def test_opposite_limit_on_position_is_reduce_only(self):
        self.client.amount=Decimal('0.2')
        t.execute(self.payload('limit_sell',limit_price='1010'))
        order=self.posts('/fapi/v1/order')[0]
        self.assertEqual((order['reduceOnly'],order['quantity']),('true','0.2'))
    def test_protected_position_blocks_limits(self):
        self.client.amount=Decimal(1);self.client.orders=[{'symbol':'BTCUSDT','side':'SELL','orderType':'STOP_MARKET','triggerPrice':'950','closePosition':True,'positionSide':'BOTH','clientAlgoId':'x','algoId':1}]
        with self.assertRaisesRegex(ValueError,'stop or target'):t.execute(self.payload(limit_price='990'))
        self.assertEqual(self.posts('/fapi/v1/order'),[])
    def test_only_usdt_favorites_allowed(self):
        for symbol in ('PEPEUSDT','BTCUSDC'):
            with self.assertRaisesRegex(ValueError,'favorite USDT'):t.execute(self.payload(symbol=symbol))
        self.assertEqual(self.posts('/fapi/v1/order')+self.posts('/fapi/v1/algoOrder'),[])
    def test_crossing_price_rejected_before_any_order(self):
        with self.assertRaisesRegex(ValueError,'post-only'):t.execute(self.payload(limit_price='1000'))
        with self.assertRaisesRegex(ValueError,'post-only'):t.execute(self.payload('limit_sell',limit_price='999'))
        self.assertEqual(self.posts('/fapi/v1/order')+self.posts('/fapi/v1/algoOrder'),[])
    def test_taker_rejection_opens_nothing(self):
        self.client.reject_maker=True
        with self.assertRaisesRegex(ValueError,'taker'):t.execute(self.payload(limit_price='990'))
        self.assertEqual(self.client.orders,[]);self.assertEqual(self.posts('/fapi/v1/algoOrder'),[])

if __name__=='__main__':unittest.main()
