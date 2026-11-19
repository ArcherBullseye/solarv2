import copy
import json
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import create_app
from app.db import init_db
from app.home.discovery import DiscoveryService, catalog, mapping, public_candidates
from app.home.lan_discovery import local_ip, network_range, scan_network
from app.home.store import ConflictError, init_home_store, load_document, save_document
from app.home import tuya_account
from app.state import AppState

KEY = '0123456789abcdef'


def devices():
    return [dict(id='gateway', name='G08Pro', category='wg2', local_key=KEY, home_id='home'),
        dict(id='sensor', name='Room sensor', sub=True, node_id='node-sensor', local_key=KEY,
             home_id='home', product_id='vtA4pDd6PLUZzXgZ'),
        dict(id='plug', name='Heater plug', sub=True, node_id='node-plug', local_key=KEY,
             home_id='home', relations=[dict(dpId=1, statusCode='switch_1', valueType='Boolean',
                                            valueConvert='default', supportLocal=True)])]


def lan():
    return dict(matches={'gateway': dict(address='192.168.88.194', version='3.4')},
                reachable=['192.168.88.194'], warnings=[])


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        p = patch('app.db.DB_PATH', Path(temp.name) / 'controller.db')
        p.start(); self.addCleanup(p.stop)
        init_db(); init_home_store()
        self.state = AppState()
        self.service = DiscoveryService(Mock(snapshot=lambda: {}), self.state)

    def ready(self, raw=None):
        self.service.candidates = catalog(raw or devices(), lan(), load_document())
        self.service.batch = 'batch'
        self.service.expires = time.time() + 60

    def test_import_includes_hub_routes_nodes_and_masks_credentials(self):
        self.ready()
        public = self.service.snapshot()
        self.assertNotIn(KEY, json.dumps(public))
        self.assertNotIn('local_key', json.dumps(public))
        doc, count, disabled = self.service.import_devices(dict(batch='batch', revision=0,
            items=[{'id':'sensor'}, {'id':'plug'}]))
        self.assertEqual((count, disabled), (3, 0))
        self.assertEqual(doc['hubs'][0]['address'], '192.168.88.194')
        self.assertEqual(doc['devices'][0]['hub_id'], doc['hubs'][0]['id'])
        self.assertEqual(doc['devices'][0]['temperature_dp'], '103')
        self.assertEqual(doc['devices'][1]['power_dp'], '1')
        self.assertEqual(doc['devices'][1]['node_id'], 'node-plug')
        self.assertTrue(self.state.home_refresh.is_set())
        # Importing twice must preserve identities and settings.
        with self.assertRaises(ValueError):
            self.service.import_devices(dict(batch='batch', revision=doc['revision'], items=[{'id':'plug'}]))
        self.assertEqual(load_document(), doc)

    def test_revision_and_batch_conflicts_do_not_write(self):
        self.ready()
        for batch, rev, error in [('old', 0, ValueError), ('batch', 9, ConflictError)]:
            with self.assertRaises(error):
                self.service.import_devices(dict(batch=batch, revision=rev, items=[{'id':'plug'}]))
            self.assertEqual(load_document()['revision'], 0)

    def test_ambiguous_hub_requires_choice_and_missing_details_disable(self):
        raw = devices()
        raw[1]['local_key'] = ''
        raw.append(dict(id='other', name='Other hub', category='wg2', local_key=KEY, home_id='home'))
        self.ready(raw)
        self.assertEqual(self.service.candidates[1]['hub_device_id'], '')
        with self.assertRaises(ValueError):
            self.service.import_devices(dict(batch='batch', revision=0, items=[{'id':'sensor'}]))
        doc, count, disabled = self.service.import_devices(dict(batch='batch', revision=0,
            items=[dict(id='sensor', hub_device_id='other')]))
        self.assertEqual((count, disabled), (2, 2))
        self.assertFalse(doc['hubs'][0]['enabled'])

    def test_existing_hub_and_rules_are_preserved(self):
        self.ready()
        first, _, _ = self.service.import_devices(dict(batch='batch', revision=0, items=[{'id':'sensor'}]))
        first['hubs'][0]['name'] = 'My custom name'
        first['devices'][0]['temperature_divisor'] = 10
        saved = save_document(first)
        self.ready()
        second, count, _ = self.service.import_devices(dict(batch='batch', revision=saved['revision'], items=[{'id':'plug'}]))
        self.assertEqual(count, 1)
        self.assertEqual(second['hubs'], saved['hubs'])
        self.assertEqual(second['devices'][0], saved['devices'][0])

    def test_private_address_and_protocol_override(self):
        raw = devices()
        self.service.candidates = catalog(raw, dict(matches={}, reachable=[]), load_document())
        self.service.batch = 'batch'
        self.service.expires = time.time()+60
        with self.assertRaises(ValueError):
            self.service.import_devices(dict(batch='batch', revision=0,items=[dict(id='gateway',address='8.8.8.8')]))
        doc, _, disabled = self.service.import_devices(dict(batch='batch', revision=0,
            items=[dict(id='gateway', address='192.168.88.194', version=3.4),dict(id='plug')]))
        self.assertEqual(disabled, 0)
        self.assertEqual(doc['hubs'][0]['version'], 3.4)

    def test_mapping_scales_and_unsupported_models(self):
        raw = {'relations': [dict(dpId=10,statusCode='temp_current',valueType='Integer',supportLocal=True,
            valueDesc=json.dumps(dict(scale=1,unit='℃'))),dict(dpId=11,statusCode='humidity_value',
            valueType='Integer',supportLocal=True,valueDesc=json.dumps(dict(scale=0,unit='%')))]}
        kind, points = mapping(raw)
        self.assertEqual(kind, 'sensor')
        self.assertEqual(points['temperature_divisor'], 10)
        del raw['relations'][0]['valueDesc']
        self.assertIsNone(mapping(raw)[0])
        plug = devices()[2]
        plug['relations'].append(dict(dpId=2,statusCode='switch_2',valueType='Boolean',supportLocal=True))
        self.assertIsNone(mapping(plug)[0])
        self.assertIsNone(mapping({'category':'cz'})[0])

    def test_account_file_is_private_and_disconnect_preserves_devices(self):
        account = {'token_info':{'access_token':'private-token'}}
        tuya_account.write_account(account)
        self.assertEqual(stat.S_IMODE(tuya_account.account_path().stat().st_mode), 0o600)
        self.assertEqual(tuya_account.read_account(), account)
        self.service.account = account
        self.ready()
        doc, _, _ = self.service.import_devices(dict(batch='batch',revision=0,items=[{'id':'sensor'}]))
        self.service.disconnect()
        self.assertFalse(tuya_account.account_path().exists())
        self.assertEqual(load_document(), doc)
        self.assertIsNone(self.service.snapshot()['qr_image'])

    def test_async_errors_hide_secrets_and_prevent_overlap(self):
        started, release = threading.Event(), threading.Event()
        def operation():
            started.set(); release.wait(2)
            raise RuntimeError('secret-token')
        self.service._start('test', operation)
        self.assertTrue(started.wait(1))
        with self.assertRaises(ValueError):
            self.service.scan('192.168.88.0/24')
        release.set()
        for _ in range(100):
            if not self.service.snapshot()['busy']: break
            time.sleep(.005)
        self.assertNotIn('secret-token', json.dumps(self.service.snapshot()))
        self.assertTrue(self.service.snapshot()['error'])

    def test_account_endpoint_normalization_and_rejection(self):
        for host in ('apigw.tuyaus.com', 'apigw.tuyaeu.com', 'openapi.tuyacn.com',
                     'apigw.tuyain.com', 'apigw-us.iotbing.com'):
            for suffix in ('', '/', ':443/'):
                with self.subTest(host=host, suffix=suffix):
                    self.assertEqual(tuya_account.normalize_endpoint('https://'+host+suffix), 'https://'+host)
        self.assertEqual(tuya_account.normalize_endpoint('HTTPS://APIGW.TUYAUS.COM/'), 'https://apigw.tuyaus.com')
        for value in (None, 123, '', 'http://apigw.tuyaus.com', 'https://192.168.88.194',
                      'https://apigw.tuyaus.com.attacker.com', 'https://not-tuyaus.com',
                      'https://attacker.com/tuyaus.com', 'https://tuyaus.com@attacker.com',
                      'https://secret@apigw.tuyaus.com', 'https://apigw.tuyaus.com:8443',
                      'https://apigw.tuyaus.com:bad', 'https://apigw.tuyaus.com/path',
                      'https://apigw.tuyaus.com/?token=secret', 'https://apigw.tuyaus.com/#fragment',
                      'https://apigw.tuyaus.com/\\path', 'https://[invalid', 'https://apigw.tuyaus.com/\n'):
            with self.subTest(endpoint=value), self.assertRaises(tuya_account.AccountError) as error:
                tuya_account.normalize_endpoint(value)
            self.assertNotIn('secret', str(error.exception))

    def test_unapproved_signin_still_requires_phone_approval(self):
        login = Mock()
        login.login_result.return_value = (False, {})
        with patch('app.home.tuya_account.sdk', return_value=(Mock(return_value=login), Mock())), \
             self.assertRaises(tuya_account.AccountError):
            tuya_account.confirm_signin({'qrcode':'pending', 'user_code':'user-code'})
        login.session.close.assert_called_once()

    def test_regional_signin_reaches_device_import(self):
        info = dict(endpoint='https://apigw.tuyaus.com/', terminal_id='terminal',
                    t=123, uid='uid', expire_time=3600, access_token='private-access',
                    refresh_token='private-refresh')
        login = Mock()
        login.login_result.return_value = (True, info)
        self.service.pending = {'qrcode':'pending-code', 'user_code':'user-code'}
        self.service.lan = lan()
        with patch('app.home.tuya_account.sdk', return_value=(Mock(return_value=login), Mock())), \
             patch('app.home.tuya_account.fetch_devices', return_value=(devices(), [])):
            self.service.confirm()
            for _ in range(200):
                if not self.service.snapshot()['busy']: break
                time.sleep(.005)
        status = self.service.snapshot()
        self.assertIsNone(status['error'])
        self.assertEqual(status['phase'], 'ready')
        self.assertEqual(self.service.account['endpoint'], 'https://apigw.tuyaus.com')
        self.assertNotIn('private-access', json.dumps(status))
        self.assertNotIn('private-refresh', json.dumps(status))
        doc, count, disabled = self.service.import_devices(dict(batch=status['batch'], revision=0,
            items=[{'id':'sensor'}, {'id':'plug'}]))
        self.assertEqual((count, disabled), (3, 0))
        self.assertEqual(len(doc['devices']), 2)
        login.session.close.assert_called_once()

    def test_qr_expiry_and_retry(self):
        with patch('app.home.tuya_account.begin_signin', return_value=({'qrcode':'secret'},'data:image/svg+xml;base64,AA==')):
            self.service.signin('user-code',False)
            for _ in range(100):
                if not self.service.snapshot()['busy']: break
                time.sleep(.005)
        self.service.expires = time.time()-1
        self.assertEqual(self.service.snapshot()['phase'], 'expired')
        with self.assertRaises(ValueError): self.service.confirm()

    def test_routes_validate_requests_and_do_not_expose_secrets(self):
        app, *_ = create_app()
        client = app.test_client()
        self.assertEqual(client.post('/api/home/discovery/scan', json={'network':'8.8.8.0/24'}).status_code,400)
        self.assertEqual(client.post('/api/home/discovery/sync',json={}).status_code,400)
        self.assertEqual(client.post('/api/home/discovery/import',json={'items':[]}).status_code,400)
        self.assertEqual(client.post('/api/home/discovery/unknown',json={}).status_code,404)
        payload = client.get('/api/home/discovery').json
        self.assertFalse(payload['connected'])
        self.assertNotIn('token_info',payload)

    def test_bounded_lan_scan_avoids_active_hub_connection(self):
        doc = dict(hubs=[dict(id='h',device_id='gateway',address='192.168.88.194',version=3.4,enabled=True)],
            devices=[dict(id='d',transport='zigbee',hub_id='h')])
        with patch('tinytuya.scanner.devices',return_value={}), patch('app.home.lan_discovery.probe') as probe:
            result=scan_network(None, doc, {'d':{'status':'connected'}})
            scan_network(None, doc, {'d':{'status':'offline'}})
        probe.assert_not_called()
        self.assertEqual(result['matches']['gateway']['address'],'192.168.88.194')
        for value in ['8.8.8.0/24','127.0.0.0/24','10.0.0.0/8','::1/128','not-a-network','192.0.2.0/24',{'network':'bad'}]:
            with self.assertRaises(ValueError): network_range(value)
        self.assertEqual(str(network_range('192.168.88.237/24')),'192.168.88.0/24')
        self.assertIsNone(local_ip('8.8.8.8'))


if __name__ == '__main__': unittest.main()
