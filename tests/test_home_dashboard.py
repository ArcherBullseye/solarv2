import copy
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, call, patch

from app import create_app
from app.config import DEFAULTS
from app.db import init_db
from app.home.engine import RuleEngine, evaluate_condition, fresh_value
from app.home.metrics import SOURCES, dashboard_readings
from app.home.runtime import HomeService
from app.home.schema import validate
from app.home.store import init_home_store, load_document, save_document
from app.state import AppState
from test_home_workspace import fixture

NOW = 1795100000


def stamp(now=NOW):
    return datetime.fromtimestamp(now, timezone.utc).isoformat()


def snapshot(now=NOW):
    return dict(readings=dict(soc=100,grid_power_w=2000,battery_power_w=500,input_power_w=6000,
        load_power_w=1500,backup_power_w=2000),last_updated=stamp(now),
        weather=dict(current_temp_f=64,current_radiation_w=450,remaining_sunny_hours=4,
            hourly=[dict(temp_f=64,radiation_w=450),dict(temp_f=65,radiation_w=500)]),
        weather_last_updated=stamp(now), miners_last_updated=stamp(now),
        miners={'X1':dict(running=True,reachable=True,hold=False,hashrate_mhs=50e6),
                'X2':dict(running=False,reachable=False,hold=True,hashrate_mhs=100e6)},miner_running=True,
        pool=dict(sats_today=10000,unpaid_sats=50000,hashrate_ths=45,hashrate_24h_ths=43),
        pool_last_updated=stamp(now),btc_price_usd=100000,btc_price_last_updated=stamp(now),
        dehum_power=False,dehum_humidity=55,dehum_tank_full=False,dehum_auto_on=False,
        dehum_last_updated=stamp(now),dehum_error=None,smart_start_active=True,effective_soc_on=80,
        smart_min_pv_w=1000,eod_target=98,eod_projected_with=99,eod_projected_without=100,
        eod_protecting=False,smart_hold_active=False,
        ramp=dict(target_total_w=2500,current_total_w=2000,dry_run=True),ramp_last_updated=stamp(now))


def solar_rule(on=True):
    return dict(id='solar-on' if on else 'solar-off',name='Solar plug ON' if on else 'Solar plug OFF',
        enabled=True,match='all' if on else 'any',for_seconds=0,cooldown_seconds=0,
        conditions=[dict(id='soc',source='solar',metric='soc',operator='at_least' if on else 'at_most',value=95 if on else 90),
                    dict(id='excess',source='solar',metric='excess_power_w',operator='at_least' if on else 'at_most',value=1000 if on else 0)],
        actions=[dict(id='switch',type='plug',device_id='device-2',on=on)])


class DashboardAutomationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher=patch('app.db.DB_PATH',Path(temp.name)/'controller.db')
        patcher.start(); self.addCleanup(patcher.stop)
        init_db();init_home_store()
        self.settings={**DEFAULTS,'battery_capacity_kwh':20,'ramp_charge_margin':1.25}

    def test_sources_conversion_and_zero_states(self):
        sources=dashboard_readings(snapshot(),self.settings)
        self.assertEqual(sources['solar']['excess_power_w'],2500)
        self.assertEqual(sources['dashboard:mining']['hashrate_ths'],50)
        self.assertNotIn('running',sources['dashboard:miner:X2'])
        self.assertIs(sources['dashboard:miner:X2']['reachable'],False)
        self.assertEqual(sources['dashboard:pool']['today_usd'],10)
        self.assertEqual(sources['dashboard:pool']['unpaid_usd'],50)
        self.assertIs(sources['dashboard:dehumidifier']['power'],False)
        self.assertEqual(sources['dashboard:weather']['next_hour_radiation_w'],500)
        self.assertEqual(sources['solar']['grid_import_w'],0)
        json.dumps(sources,allow_nan=False)
        for source, row in sources.items():
            self.assertTrue(set(row)-{'status','updated_at','max_age_seconds'} <= set(SOURCES[source]['metrics']))

    def test_excess_uses_charge_reserve_and_signed_import_discharge(self):
        data=snapshot()
        data['readings'].update(soc=90,grid_power_w=-100,battery_power_w=1000)
        result=dashboard_readings(data,self.settings)['solar']
        # (98-90)% of 20kWh / four hours, with 25% margin = 500W.
        self.assertEqual(result['charge_reserve_w'],500)
        self.assertEqual(result['excess_power_w'],400)
        self.assertEqual(result['grid_import_w'],100)
        self.assertEqual(result['grid_export_w'],0)
        data['readings']['battery_power_w']=-600
        result=dashboard_readings(data,self.settings)['solar']
        self.assertEqual(result['excess_power_w'],-1200)
        self.assertEqual(result['battery_charge_w'],0)
        self.assertEqual(result['battery_discharge_w'],600)

    def test_each_source_retains_its_own_freshness(self):
        data=snapshot()
        data['weather_last_updated']=stamp(NOW-1800)
        data['miners_last_updated']=stamp(NOW-301)
        data['pool_last_updated']=stamp(NOW-901)
        data['readings']['soc']=90
        sources=dashboard_readings(data,self.settings)
        self.assertEqual(fresh_value(sources['solar'],'excess_power_w',NOW),2000)
        self.assertIsNone(fresh_value(sources['dashboard:mining'],'hashrate_ths',NOW))
        self.assertIsNone(fresh_value(sources['dashboard:pool'],'today_usd',NOW))
        self.assertEqual(fresh_value(sources['dashboard:pool'],'btc_price_usd',NOW),100000)
        self.assertEqual(fresh_value(sources['dashboard:weather'],'remaining_sunny_hours',NOW),4)
        data['weather_last_updated']=stamp(NOW-3601)
        sources=dashboard_readings(data,self.settings)
        self.assertNotIn('excess_power_w',sources['solar'])
        data['readings']['soc']=100
        sources=dashboard_readings(data,self.settings)
        self.assertEqual(sources['solar']['excess_power_w'],2500)
        self.assertIsNone(fresh_value(sources['solar'],'soc',NOW+301))

    def test_failures_and_invalid_values_never_become_false_zeroes(self):
        data=snapshot()
        data.update(readings=None,dehum_error='offline',miners_last_updated=None)
        sources=dashboard_readings(data,self.settings)
        self.assertNotIn('soc',sources['solar'])
        self.assertNotIn('excess_power_w',sources['solar'])
        self.assertNotIn('power',sources['dashboard:dehumidifier'])
        self.assertNotIn('hashrate_ths',sources['dashboard:mining'])
        for value in (True,float('nan'),float('inf'),'100'):
            data=snapshot();data['readings']['soc']=value
            sources=dashboard_readings(data,self.settings)
            self.assertIsNone(evaluate_condition(dict(source='solar',metric='soc',operator='above',value=90),
                {},sources['solar'],'America/Detroit',NOW,sources))

    def test_comparisons_include_full_soc_and_ranges(self):
        data=snapshot();sources=dashboard_readings(data,self.settings)
        condition=dict(source='solar',metric='soc',value=100,end=101)
        expected={'at_least':True,'at_most':True,'equals':True,'above':False,'below':False,'between':True,'outside':False}
        for op,want in expected.items():
            self.assertIs(evaluate_condition(dict(condition,operator=op),{},sources['solar'],'America/Detroit',NOW,sources),want)
        c=dict(source='dashboard:dehumidifier',metric='power',operator='is_off')
        self.assertTrue(evaluate_condition(c,{},sources['solar'],'America/Detroit',NOW,sources))

    def test_validation_and_backward_compatibility(self):
        doc=fixture()
        doc['rules']=[solar_rule(True),solar_rule(False)]
        validated=validate(doc,dict(revision=0,hubs=[],devices=[]))
        self.assertFalse(validated['rules'][1]['actions'][0]['on'])
        for source, definition in SOURCES.items():
            if source=='clock': continue
            for key, metric in definition['metrics'].items():
                candidate=copy.deepcopy(doc)
                candidate['rules'][0]['conditions']=[dict(id='c',source=source,metric=key,
                    operator='is_on' if metric['kind']=='boolean' else 'above',value=1)]
                validate(candidate,doc)
        ranged=copy.deepcopy(doc)
        ranged['rules'][0]['conditions'][0].update(operator='between',value=90,end=100)
        self.assertEqual(validate(ranged,doc)['rules'][0]['conditions'][0]['end'],100)
        ranged['rules'][0]['conditions'][0]['end']=80
        with self.assertRaises(ValueError):validate(ranged,doc)
        self.assertEqual(validate(fixture(),doc)['rules'],fixture()['rules'])

    def test_soc_and_excess_crossings_queue_on_and_off_without_startup_actions(self):
        doc=fixture();doc['rules']=[solar_rule(True),solar_rule(False)]
        service=HomeService(AppState());service.document=save_document(doc)
        service.command=Mock()
        engine=RuleEngine()
        for elapsed,soc,grid in [(0,80,0),(1,100,2000),(2,100,2000),(3,89,-100)]:
            data=snapshot(NOW+elapsed);data['readings'].update(soc=soc,grid_power_w=grid)
            sources=dashboard_readings(data,self.settings)
            engine.tick(service.document,{},sources['solar'],'America/Detroit',service.action,Mock(),NOW+elapsed,dashboard=sources)
        self.assertEqual(service.command.call_args_list,[call('device-2',True,'Solar plug ON',automatic=True),
            call('device-2',False,'Solar plug OFF',automatic=True)])

    def test_preview_and_runtime_share_the_dashboard_adapter(self):
        app,state,*_=create_app()
        doc=fixture();doc['rules']=[solar_rule(True)]
        save_document(doc)
        state.update(snapshot(time.time()))
        client=app.test_client()
        payload=client.get('/api/home').json
        self.assertIn('excess_power_w',payload['condition_sources']['solar']['metrics'])
        self.assertIn('dashboard:weather',payload['dashboard'])
        self.assertEqual(client.get('/api/home/preview/solar-on').json['result'],True)
        state.set('readings',None)
        self.assertIsNone(client.get('/api/home/preview/solar-on').json['result'])


if __name__=='__main__':unittest.main()
