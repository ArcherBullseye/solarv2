"""Visibility is saved presentation metadata, independent of device control."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
from app.db import init_db
from app.home.groups import execution_document
from app.home.runtime import HomeService
from app.home.store import init_home_store, load_document, save_document
from app.state import AppState
from test_home_workspace import fixture


class DashboardSelectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = patch('app.db.DB_PATH', Path(temp.name)/'controller.db')
        patcher.start()
        self.addCleanup(patcher.stop)
        init_db()
        init_home_store()

    def test_default_all_sensors_and_explicit_empty_are_different(self):
        service = HomeService(AppState())
        saved = save_document(fixture())
        self.assertNotIn('dashboard_devices', saved)
        service.document = saved
        service.publish()
        self.assertEqual(service.state.get('home_dashboard_device_ids'), ['device-0','device-1'])
        saved['dashboard_devices'] = []
        service.document = save_document(saved)
        service.publish()
        self.assertEqual(service.state.get('home_dashboard_device_ids'), [])
        self.assertEqual(len(service.state.get('home_devices')), 6)

    def test_selection_roundtrip_with_all_device_types_and_saved_order(self):
        doc = fixture()
        doc['devices'][3]['kind'] = 'dehumidifier'
        doc['dashboard_devices'] = ['device-3','device-2','device-0']
        saved = save_document(doc)
        service = HomeService(AppState())
        service.document = saved
        service.publish()
        self.assertEqual(load_document()['dashboard_devices'], doc['dashboard_devices'])
        self.assertEqual(service.state.get('home_dashboard_device_ids'), ['device-0','device-2','device-3'])
        self.assertEqual(service.document['rules'], saved['rules'])

    def test_invalid_visibility_choices_are_rejected_atomically(self):
        saved = save_document(fixture())
        for value in (None, True, 'device-0', {}, ['device-0','device-0'], ['missing'], [123], [{}]):
            bad = copy.deepcopy(saved)
            bad['dashboard_devices'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                save_document(bad)
            self.assertEqual(load_document(), saved)

    def test_older_clients_preserve_selection_and_removed_devices_are_pruned(self):
        doc = fixture()
        doc['dashboard_devices'] = ['device-0','device-5']
        saved = save_document(doc)
        saved.pop('dashboard_devices')
        saved = save_document(saved)
        self.assertEqual(saved['dashboard_devices'], ['device-0','device-5'])
        saved.pop('dashboard_devices')
        saved['devices'] = [d for d in saved['devices'] if d['id'] != 'device-5']
        self.assertEqual(save_document(saved)['dashboard_devices'], ['device-0'])

    def test_selection_only_save_keeps_workers_readings_holds_and_engine_memory(self):
        save_document(fixture())
        service = HomeService(AppState())
        with patch('app.home.runtime.ConnectionWorker.start'):
            service.tick()
            service.report('device-0', {'temperature_f':40,'humidity_pct':54})
            service.readings['device-2'].update(hold_until=9999999999)
            service.tick()
            workers = list(service.workers)
            engine, memory = service.engine, copy.deepcopy(service.engine.baselines)
            readings = service.snapshot()
            saved = load_document()
            before = execution_document(saved)
            saved['dashboard_devices'] = ['device-2']
            saved = save_document(saved)
            self.assertEqual(execution_document(saved), before)
            service.tick()
        self.assertIs(service.engine, engine)
        self.assertEqual(service.engine.baselines, memory)
        self.assertEqual(service.snapshot(), readings)
        self.assertEqual(service.workers, workers)
        self.assertTrue(all(not w.stop_event.is_set() for w in workers))
        self.assertTrue(all(w.revision == saved['revision'] for w in workers))
        self.assertEqual(service.state.get('home_dashboard_device_ids'), ['device-2'])

    def test_settings_and_status_api_expose_saved_visibility_without_credentials(self):
        app, state, *_ = create_app()
        client = app.test_client()
        doc = fixture()
        doc['dashboard_devices'] = ['device-1']
        result = client.post('/api/home', json=doc)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['document']['dashboard_devices'], ['device-1'])
        service = app.extensions['home_service']
        service.document = load_document()
        service.publish()
        status = client.get('/api/status').json
        self.assertEqual(status['home_dashboard_device_ids'], ['device-1'])
        self.assertNotIn('local_key',str(status['home_devices']))
        self.assertIn('Dashboard card',client.get('/home').text)
        self.assertIn('/home#dashboard',client.get('/').text)
