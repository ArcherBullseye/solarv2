"""A cached relay state is not proof that a Zigbee plug is still reachable."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.db import init_db
from app.home.runtime import ConnectionWorker, HomeService
from app.home.store import init_home_store, save_document
from app.state import AppState
from test_home_workspace import fixture


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        p=patch('app.db.DB_PATH',Path(temp.name)/'controller.db');p.start();self.addCleanup(p.stop)
        init_db();init_home_store()
        self.service=HomeService(AppState())
        self.doc=save_document(fixture());self.service.document=self.doc
        self.worker=ConnectionWorker(self.service,self.doc['hubs'][0],self.doc['devices'],self.doc['revision'])
        self.worker.children={d['id']:Mock() for d in self.doc['devices']}
        self.plug=self.doc['devices'][2]

    def test_hub_offline_report_survives_repeated_cached_off_states(self):
        self.worker.route({'reqType':'subdev_online_stat_report','data':{'online':[],'offline':['node-2']}})
        for _ in range(3):
            self.worker.accept(self.plug,{'cid':'node-2','dps':{'1':False,'19':0}})
        row=self.service.snapshot()['device-2']
        self.assertEqual(row['status'],'offline')
        self.assertFalse(row['power'])

    def availability(self, online=(), offline=()):
        self.worker.route({'reqType':'subdev_online_stat_report','data':{'online':list(online),'offline':list(offline)}})

    def test_cached_off_at_startup_is_not_proof_that_plug_is_online(self):
        self.worker.accept(self.plug,{'cid':'node-2','dps':{'1':False}})
        self.assertEqual(self.service.snapshot()['device-2']['status'],'waiting')
        self.assertTrue(self.service.snapshot()['device-2']['availability_required'])

    def test_online_status_recovers_and_an_offline_sibling_stays_offline(self):
        self.availability(online=['node-2'],offline=['node-3'])
        self.worker.accept(self.plug,{'cid':'node-2','dps':{'1':False}})
        self.worker.accept(self.doc['devices'][3],{'cid':'node-3','dps':{'1':False}})
        self.assertEqual(self.service.snapshot()['device-2']['status'],'connected')
        self.assertEqual(self.service.snapshot()['device-3']['status'],'offline')
        self.availability(online=['node-3'])
        self.assertEqual(self.service.snapshot()['device-3']['status'],'connected')

    def test_positive_availability_expires_even_when_cached_reports_continue(self):
        from datetime import datetime,timezone
        import time
        self.availability(online=['node-2'])
        self.service.readings['device-2']['zigbee_online_at']=datetime.fromtimestamp(time.time()-91,timezone.utc).isoformat()
        self.worker.accept(self.plug,{'cid':'node-2','dps':{'1':False}})
        self.assertEqual(self.service.snapshot()['device-2']['status'],'waiting')
        self.availability(online=['node-2'])
        self.assertEqual(self.service.snapshot()['device-2']['status'],'connected')

    def test_partial_lists_missing_nodes_and_nearby_do_not_infer_availability(self):
        self.availability(offline=['node-2'])
        self.worker.route({'reqType':'subdev_online_stat_report','data':{'online':['node-3'],'nearby':['node-2']}})
        self.worker.accept(self.plug,{'cid':'node-2','dps':{'1':False}})
        self.assertEqual(self.service.snapshot()['device-2']['status'],'offline')
        self.assertEqual(self.service.snapshot()['device-3']['status'],'connected')
        self.assertNotIn('device-4',self.service.snapshot())
        self.availability(online=['node-2'],offline=['node-2'])
        self.assertEqual(self.service.snapshot()['device-2']['status'],'offline')

    def test_malformed_or_unrelated_reports_are_not_online_evidence(self):
        self.worker.accept(self.plug,{'cid':'node-2','dps':{'1':False}})
        for data in (None, [], {'online':'node-2'}, {'offline':[{}]}, {'nearby':['node-2']}):
            self.worker.route({'reqType':'subdev_online_stat_report','data':data})
        self.worker.route({'reqType':'unrelated','data':{'online':['node-2']}})
        self.assertEqual(self.service.snapshot()['device-2']['status'],'waiting')

    def test_empty_powered_poll_marks_offline_and_sensor_sleep_does_not(self):
        self.availability(online=['node-2'])
        self.worker.children['device-2'].status.return_value=None
        self.worker.poll_device(self.plug)
        self.assertEqual(self.service.snapshot()['device-2']['status'],'offline')
        self.worker.children['device-0'].status.return_value=None
        self.worker.poll_device(self.doc['devices'][0])
        self.assertNotIn('device-0',self.service.snapshot())

    def test_query_uses_existing_gateway_and_routes_interleaved_reports(self):
        self.worker.root=Mock()
        self.worker.root.subdev_query.return_value={'reqType':'subdev_online_stat_report','data':{'offline':['node-2']}}
        self.worker.query_availability()
        self.worker.root.subdev_query.assert_called_once_with()
        self.assertEqual(self.service.snapshot()['device-2']['status'],'offline')
        self.worker.root.subdev_query.return_value={'cid':'node-1','dps':{'103':18,'101':56}}
        self.worker.query_availability()
        self.assertEqual(self.service.snapshot()['device-1']['temperature_f'],64.4)
        self.worker.root.subdev_query.side_effect=ConnectionError('unsupported')
        self.worker.query_availability()
        self.assertEqual(self.service.snapshot()['device-2']['status'],'offline')
        self.worker.root.set_value.assert_not_called()
        self.assertTrue(all(child.set_value.call_count==0 for child in self.worker.children.values()))

    def test_availability_frame_returned_to_child_query_is_not_a_relay_report(self):
        self.worker.children['device-2'].status.return_value={'reqType':'subdev_online_stat_report','data':{'online':['node-3'],'offline':['node-2']}}
        self.worker.poll_device(self.plug)
        self.assertEqual(self.service.snapshot()['device-2']['status'],'offline')
        self.assertEqual(self.service.snapshot()['device-3']['status'],'connected')
        self.assertNotIn('power',self.service.snapshot()['device-3'])

    def test_poll_error_cannot_be_cleared_by_cached_dps_without_new_online_check(self):
        self.availability(online=['node-2'])
        self.worker.children['device-2'].status.return_value={'Err':'902'}
        self.worker.poll_device(self.plug)
        self.worker.accept(self.plug,{'cid':'node-2','dps':{'1':False}})
        self.assertNotEqual(self.service.snapshot()['device-2']['status'],'connected')
        self.availability(online=['node-2'])
        self.assertEqual(self.service.snapshot()['device-2']['status'],'connected')

    def test_snapshot_blocks_rules_on_offline_plug_and_publishes_same_status(self):
        from app.home.engine import fresh_value
        import time
        self.availability(offline=['node-2'])
        self.worker.accept(self.plug,{'cid':'node-2','dps':{'1':False}})
        row=self.service.snapshot()['device-2']
        self.assertIsNone(fresh_value(row,'power',time.time()))
        self.assertEqual(self.service.state.get('home_devices')['device-2']['status'],'offline')
        self.assertEqual(self.service.device_info('device-2')['readings']['status'],'offline')

    def test_cached_matching_state_cannot_confirm_a_power_command_while_offline(self):
        self.availability(offline=['node-2'])
        self.worker.children['device-2'].set_value.return_value={'dps':{'1':False}}
        self.worker.children['device-2'].status.return_value={'cid':'node-2','dps':{'1':False}}
        self.service.device_workers={'device-2':self.worker}
        self.service.command('device-2',False)
        self.worker.drain_commands()
        self.assertIsNotNone(self.service.snapshot()['device-2']['command_error'])
        self.assertFalse(self.service.snapshot()['device-2']['pending'])

    def test_wifi_and_battery_sensors_keep_their_existing_liveness_behavior(self):
        self.service.report('device-0',{'temperature_f':64.4,'humidity_pct':54})
        self.assertEqual(self.service.snapshot()['device-0']['status'],'connected')
        self.service.document['devices'][2]['transport']='wifi'
        self.service.report('device-2',{'power':False})
        self.assertEqual(self.service.snapshot()['device-2']['status'],'connected')
        self.assertNotIn('availability_required',self.service.snapshot()['device-2'])

    def test_malformed_or_future_online_timestamp_stays_unconfirmed(self):
        from datetime import datetime,timezone
        import time
        self.availability(online=['node-2'])
        for stamp in (None,'invalid',datetime.fromtimestamp(time.time()+3600,timezone.utc).isoformat()):
            self.service.readings['device-2']['zigbee_online_at']=stamp
            self.worker.accept(self.plug,{'cid':'node-2','dps':{'1':False}})
            self.assertEqual(self.service.snapshot()['device-2']['status'],'waiting')

    def test_disabled_plug_is_not_reclassified_as_waiting(self):
        self.service.document['devices'][2]['enabled']=False
        self.service.readings['device-2']={'status':'disabled','updated_at':{}}
        self.assertEqual(self.service.snapshot()['device-2']['status'],'disabled')
