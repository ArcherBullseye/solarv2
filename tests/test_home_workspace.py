import copy
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import create_app
from app.config import DEFAULTS
from app.db import init_db, update_settings
from app.home.engine import RuleEngine, evaluate_condition
from app.home.runtime import ConnectionWorker, HomeService
from app.home.schema import public_document, validate
from app.home.store import ConflictError, init_home_store, load_document, save_document
from app.state import AppState


def fixture():
    hub = dict(id='hub', name='Local hub', enabled=True, address='192.168.1.10',
               device_id='hub-id', local_key='1234567890abcdef', version=3.4)
    devices = []
    for index, kind in enumerate(('sensor', 'sensor', 'plug', 'plug', 'plug', 'plug')):
        devices.append(dict(id='device-' + str(index), name=kind + str(index), enabled=True,
            kind=kind, transport='zigbee', hub_id='hub', device_id='tuya-' + str(index), node_id='node-' + str(index),
            temperature_dp='103', humidity_dp='101', power_dp='1', temperature_divisor=1,
            humidity_divisor=1, temperature_unit='C'))
    rule = dict(id='rule', name='Cold room', enabled=True, match='all', for_seconds=5, cooldown_seconds=60,
        conditions=[dict(id='cold', source='device-0', metric='temperature_f', operator='below', value=50)],
        actions=[dict(id='plug', type='plug', device_id='device-2', on=True),
                 dict(id='notify', type='telegram', message='Room is cold')])
    return dict(revision=0, timezone='America/Detroit', hubs=[hub], devices=devices, rules=[rule])


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        patcher = patch('app.db.DB_PATH', Path(self.temp.name) / 'controller.db')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)
        init_db()
        init_home_store()

    def test_multiple_devices_and_secret_roundtrip_conflict(self):
        first = save_document(fixture())
        self.assertEqual(len(first['devices']), 6)
        public = public_document(first)
        self.assertEqual(public['hubs'][0]['local_key'], '••••')
        self.assertNotIn('1234567890abcdef', json.dumps(public))
        public['devices'].reverse()
        second = save_document(public)
        self.assertEqual(second['hubs'][0]['local_key'], first['hubs'][0]['local_key'])
        self.assertEqual(second['devices'][0]['id'], 'device-5')
        with self.assertRaises(ConflictError):
            save_document(public)
        self.assertEqual(load_document(), second)

    def test_invalid_config_is_atomic(self):
        valid = save_document(fixture())
        for mutation in ('reference', 'duplicate', 'key', 'divisor', 'ip', 'timezone'):
            bad = copy.deepcopy(valid)
            if mutation == 'reference': bad['devices'].pop(0)
            if mutation == 'duplicate': bad['devices'][1]['node_id'] = 'node-0'
            if mutation == 'key': bad['hubs'][0]['local_key'] = 'wrong'
            if mutation == 'divisor': bad['devices'][0]['temperature_divisor'] = 0
            if mutation == 'ip': bad['hubs'][0]['address'] = 'not-a-lan-ip'
            if mutation == 'timezone': bad['timezone'] = 'Made/Up'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                save_document(bad)
            self.assertEqual(load_document(), valid)

    def test_existing_sensor_and_alerts_migrate_once(self):
        # Simulate the first startup against a configured v2.0.8 database.
        from app.db import _connect
        with _connect() as conn:
            conn.execute('DELETE FROM home_workspace')
        update_settings({'home_hub_enabled': True, 'home_hub_id': 'old-hub', 'home_hub_ip': '192.168.1.2',
            'home_hub_key': 'original-key-123', 'home_sensor_id': 'old-sensor', 'home_sensor_node': 'old-node',
            'home_temperature_alert_enabled': True, 'home_temperature_alert_low_f': 32,
            'home_temperature_alert_high_f': 90})
        init_home_store()
        migrated = load_document()
        self.assertEqual(migrated['devices'][0]['device_id'], 'old-sensor')
        self.assertEqual(migrated['hubs'][0]['local_key'], 'original-key-123')
        self.assertEqual(len(migrated['rules']), 2)
        update_settings({'home_sensor_id': 'different'})
        init_home_store()
        self.assertEqual(load_document(), migrated)

    def test_endpoints_save_and_mask_keys(self):
        app, *_ = create_app()
        c = app.test_client()
        self.assertEqual(c.get('/home').status_code, 200)
        response = c.post('/api/home', json=fixture())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['document']['hubs'][0]['local_key'], '••••')
        self.assertEqual(c.post('/api/home', json=fixture()).status_code, 409)
        self.assertEqual(c.post('/api/home/devices/device-2/power', json={'on':'false'}).status_code, 400)
        self.assertIsNone(c.get('/api/home/preview/rule').json['result'])

    def test_gateway_reports_are_routed_to_the_correct_sensor(self):
        service = HomeService(AppState())
        doc = save_document(fixture())
        service.document = doc
        worker = ConnectionWorker(service, doc['hubs'][0], doc['devices'], doc['revision'])
        worker.children = {d['id']: object() for d in doc['devices']}
        worker.route({'data': {'cid':'node-1','dps':{'103':18,'101':54}}})
        self.assertNotIn('device-0', service.snapshot())
        self.assertEqual(service.snapshot()['device-1']['temperature_f'], 64.4)
        worker.accept(doc['devices'][0], {'cid':'node-1','dps':{'101':55}})
        self.assertEqual(service.snapshot()['device-1']['humidity_pct'], 55)
        self.assertNotIn('device-0', service.snapshot())

    def test_one_offline_child_does_not_block_its_gateway(self):
        service = HomeService(AppState())
        doc = save_document(fixture())
        service.document = doc
        worker = ConnectionWorker(service, doc['hubs'][0], doc['devices'], doc['revision'])
        missing, online = Mock(), Mock()
        missing.status.return_value = {'Err': '902'}
        online.status.return_value = {'cid': 'node-1', 'dps': {'103': 18}}
        worker.children = {d['id']: Mock() for d in doc['devices']}
        worker.children.update({'device-0': missing, 'device-1': online})
        worker.poll_device(doc['devices'][0])
        worker.poll_device(doc['devices'][1])
        self.assertEqual(service.snapshot()['device-0']['status'], 'offline')
        self.assertEqual(service.snapshot()['device-1']['temperature_f'], 64.4)

    def test_manual_hold_and_command_confirmation(self):
        service = HomeService(AppState())
        doc = save_document(fixture())
        service.document = doc
        service.readings = {d['id']: {'updated_at':{}} for d in doc['devices']}
        worker = ConnectionWorker(service, doc['hubs'][0], doc['devices'], doc['revision'])
        child = Mock()
        child.set_value.return_value = {'dps': {'1':True}}
        child.status.return_value = {'dps': {'1':True}, 'cid':'node-2'}
        worker.children = {'device-2': child}
        service.device_workers = {'device-2': worker}
        service.command('device-2', True)
        self.assertTrue(service.snapshot()['device-2']['pending'])
        with self.assertRaises(ValueError):
            service.command('device-2', False, automatic=True)
        worker.drain_commands()
        with patch('app.home.runtime.ConnectionWorker.start'):
            restarted = HomeService(AppState())
            restarted.tick()
        self.assertGreater(restarted.snapshot()['device-2']['hold_until'], time.time())
        self.assertTrue(service.snapshot()['device-2']['power'])
        self.assertFalse(service.snapshot()['device-2']['pending'])
        child.status.return_value = {'dps': {'1':True}, 'cid':'node-2'}
        service.command('device-2', False)
        worker.drain_commands()
        self.assertIsNotNone(service.snapshot()['device-2']['command_error'])
        self.assertTrue(service.snapshot()['device-2']['power'])


class RuleEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = RuleEngine()
        self.doc = fixture()
        self.doc['rules'][0]['run_mode'] = 'crossing'
        self.action = Mock()
        self.log = Mock()
        self.now = 1795100000

    def tick(self, value, elapsed, stamp_elapsed=None):
        updated = self.now + (elapsed if stamp_elapsed is None else stamp_elapsed)
        devices = {'device-0': {'status':'connected', 'temperature_f':value,
            'updated_at':{'temperature_f':datetime.fromtimestamp(updated, timezone.utc).isoformat()}}}
        self.engine.tick(self.doc, devices, {}, 'America/Detroit', self.action, self.log, self.now + elapsed)

    def test_hold_cooldown_rearm_and_initial_baseline(self):
        self.tick(60, 0)
        self.tick(40, 1)
        self.tick(40, 5)
        self.action.assert_not_called()
        self.tick(40, 6)
        self.assertEqual(self.action.call_count, 2)
        self.tick(40, 10)
        self.assertEqual(self.action.call_count, 2)
        self.tick(60, 11)
        self.tick(40, 12)
        self.tick(40, 17)
        self.assertEqual(self.action.call_count, 2)
        self.tick(60, 65)
        self.tick(40, 66)
        self.tick(40, 72)
        self.assertEqual(self.action.call_count, 4)

    def test_stale_reading_and_recovery_establish_new_baseline(self):
        self.tick(60, 0)
        self.tick(40, 2000, 0)
        self.tick(40, 2001)
        self.tick(40, 2007)
        self.action.assert_not_called()
        self.tick(60, 2008)
        self.tick(40, 2009)
        self.tick(40, 2015)
        self.assertEqual(self.action.call_count, 2)

    def test_time_window_overnight_and_plug_state(self):
        c = dict(source='clock',metric='time',operator='between',value='22:00',end='06:00')
        now = datetime(2026,11,19,4,tzinfo=timezone.utc).timestamp() # 23:00 Detroit
        self.assertTrue(evaluate_condition(c, {}, {}, 'America/Detroit', now))
        self.assertFalse(evaluate_condition(c, {}, {}, 'America/Detroit', now+12*3600))
        p = dict(source='plug',metric='power',operator='is_off')
        state = {'plug': {'status':'connected','power':False,'updated_at':{'power':datetime.fromtimestamp(now,timezone.utc).isoformat()}}}
        self.assertTrue(evaluate_condition(p, state, {}, 'America/Detroit', now))


if __name__ == '__main__':
    unittest.main()
