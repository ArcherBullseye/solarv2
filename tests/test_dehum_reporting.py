"""A missing/partial pan reading must not be reported as an empty pan."""
import tempfile
import time
import unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import Mock,patch

from app import create_app
from app.clients.tuya import DehumidifierClient
from app.db import init_db,update_settings
from app.devices.dehumidifier import run_auto_control
from app.home.engine import fresh_value
from app.home.metrics import dashboard_readings
from app.home.runtime import HomeService
from app.home.store import init_home_store,save_document
from app.state import AppState
from test_home_dehumidifier import document


class DehumidifierReportingTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        p=patch('app.db.DB_PATH',Path(temp.name)/'controller.db');p.start();self.addCleanup(p.stop)
        init_db();init_home_store()

    def test_home_pan_full_is_published_even_without_a_power_reading_and_retained_offline(self):
        update_settings(dict(dehum_device_id='tuya-2',dehum_ip='192.168.1.50'))
        app,state,*_=create_app()
        service=app.extensions['home_service'];service.document=save_document(document())
        service.report('device-2',dict(tank_full=True))
        data=app.test_client().get('/api/status').json
        self.assertTrue(data['dehum_tank_full'])
        self.assertIsNone(data['dehum_power'])
        self.assertIsNotNone(data['dehum_tank_last_updated'])
        service.connection_status('device-2','offline')
        data=app.test_client().get('/api/status').json
        self.assertTrue(data['dehum_tank_full'])
        self.assertTrue(data['dehum_error'])
        self.assertIsNone(data['dehum_power'])

    def test_power_and_humidity_reports_do_not_refresh_a_stale_pan_reading(self):
        update_settings(dict(dehum_device_id='tuya-2',dehum_ip='192.168.1.50'))
        state=AppState();service=HomeService(state);service.document=save_document(document())
        service.report('device-2',dict(tank_full=True))
        old=datetime.fromtimestamp(time.time()-301,timezone.utc).isoformat()
        service.readings['device-2']['updated_at']['tank_full']=old
        service.report('device-2',dict(power=True,humidity_pct=54))
        self.assertTrue(state.get('dehum_tank_full'))
        self.assertEqual(state.get('dehum_tank_last_updated'),old)
        rows=dashboard_readings(state.snapshot(),{})
        self.assertIsNone(fresh_value(rows['dashboard:dehumidifier'],'tank_full',time.time()))
        self.assertIs(fresh_value(rows['dashboard:dehumidifier'],'power',time.time()),True)
        service.report('device-2',dict(tank_full=False))
        self.assertFalse(state.get('dehum_tank_full'))
        self.assertNotEqual(state.get('dehum_tank_last_updated'),old)

    def test_legacy_decoder_preserves_real_true_false_and_missing_unknown(self):
        client=DehumidifierClient('id','192.168.1.50','0123456789abcdef')
        device=Mock()
        with patch.object(client,'_device',return_value=device):
            for raw,want in ((True,True),(False,False),(1,None),('false',None),(None,None)):
                device.status.return_value={'dps':{'1':False,'111':raw}}
                with self.subTest(raw=raw):self.assertIs(client.get_status()['tank_full'],want)
            device.status.return_value={'dps':{'1':False}}
            self.assertIsNone(client.get_status()['tank_full'])

    def test_partial_legacy_report_retains_full_pan_and_does_not_autostart(self):
        state=AppState();state.update(dict(dehum_tank_full=True,dehum_tank_last_updated='2026-11-20T10:00:00+00:00'))
        device=Mock();device.read.return_value=dict(reachable=True,power=False,humidity=54,tank_full=None)
        run_auto_control(device,dict(dehum_auto_enabled=True),state,dict(grid_power_w=2000))
        self.assertTrue(state.get('dehum_tank_full'))
        self.assertEqual(state.get('dehum_tank_last_updated'),'2026-11-20T10:00:00+00:00')
        device.set_power.assert_not_called()
        device.read.return_value['tank_full']=False
        run_auto_control(device,{},state,None)
        self.assertFalse(state.get('dehum_tank_full'))
        self.assertNotEqual(state.get('dehum_tank_last_updated'),'2026-11-20T10:00:00+00:00')

    def test_missing_pan_report_starts_unknown_and_owner_removal_clears_old_warning(self):
        self.assertIsNone(AppState().get('dehum_tank_full'))
        update_settings(dict(dehum_device_id='tuya-2',dehum_ip='192.168.1.50'))
        state=AppState();service=HomeService(state);service.document=save_document(document())
        service.report('device-2',dict(tank_full=True))
        service.document['devices'][2]['enabled']=False
        service.publish()
        self.assertIsNone(state.get('dehum_tank_full'))
        self.assertIsNone(state.get('dehum_tank_last_updated'))
