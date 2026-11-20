import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import create_app
from app.config import DEFAULTS
from app.control.loop import ControlLoop
from app.db import init_db, update_settings
from app.devices.miner import MinerFleet
from app.home.discovery import DiscoveryService, catalog, mapping
from app.home.metrics import definitions
from app.home.ownership import legacy_owner
from app.home.runtime import ConnectionWorker, HomeService, decoded_values
from app.home.schema import public_document
from app.home.store import init_home_store, load_document, save_document
from app.state import AppState
from test_home_workspace import fixture


def document():
    doc=fixture()
    doc['devices'][2].update(kind='dehumidifier',name='Ivation 50 Pint',transport='wifi',hub_id='',node_id='',
        address='192.168.1.50',local_key='0123456789abcdef',humidity_dp='109',target_humidity_dp='3',
        tank_full_dp='111',target_humidity_divisor=1,version=3.4)
    return doc


class DehumidifierTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        p=patch('app.db.DB_PATH',Path(temp.name)/'controller.db');p.start();self.addCleanup(p.stop)
        init_db();init_home_store()
        self.doc=document()

    def test_kind_mapping_and_existing_power_rules_roundtrip(self):
        saved=save_document(self.doc)
        public=public_document(saved)
        self.assertNotIn('0123456789abcdef',json.dumps(public))
        again=save_document(public)
        device=again['devices'][2]
        self.assertEqual(device['local_key'],'0123456789abcdef')
        self.assertEqual(device['target_humidity_dp'],'3')
        self.assertEqual(again['rules'],saved['rules'])
        self.assertEqual(set(definitions(device['id'],device)),{'power','humidity_pct','target_humidity_pct','tank_full'})

    def test_optional_readings_and_bad_mapping_validation(self):
        device=self.doc['devices'][2]
        device.update(humidity_dp='',target_humidity_dp='',tank_full_dp='')
        saved=save_document(self.doc)
        self.assertEqual(saved['devices'][2]['humidity_dp'],'')
        for field,value in [('tank_full_dp','bitmask'),('target_humidity_dp','0'),('target_humidity_divisor',0)]:
            bad=copy.deepcopy(saved);bad['devices'][2][field]=value
            with self.assertRaises(ValueError):save_document(bad)
        self.assertEqual(load_document(),saved)

    def test_decode_partial_reports_and_real_false_zero_values(self):
        device=self.doc['devices'][2]
        self.assertEqual(decoded_values({'1':False,'109':0,'3':50,'111':False},device),
                         dict(power=False,humidity_pct=0,target_humidity_pct=50,tank_full=False))
        self.assertEqual(decoded_values({'109':54},device),{'humidity_pct':54})
        self.assertEqual(decoded_values({'1':'false','109':True,'3':101,'111':1},device),{})
        self.assertEqual(decoded_values({'109':float('nan')},device),{})
        device.update(humidity_divisor=10,target_humidity_divisor=1)
        self.assertEqual(decoded_values({'109':543,'3':50},device),dict(humidity_pct=54.3,target_humidity_pct=50))

    def test_freshness_and_partial_reports_do_not_erase_power_or_tank(self):
        service=HomeService(AppState());service.document=save_document(self.doc)
        worker=ConnectionWorker(service,service.document['devices'][2],[service.document['devices'][2]],service.document['revision'])
        worker.accept(service.document['devices'][2],{'dps':{'1':False,'109':54,'111':True}})
        worker.accept(service.document['devices'][2],{'dps':{'109':55}})
        row=service.snapshot()['device-2']
        self.assertFalse(row['power']);self.assertTrue(row['tank_full']);self.assertEqual(row['humidity_pct'],55)
        self.assertNotIn('temperature_f',row)

    def test_power_commands_are_verified_and_tank_full_blocks_start_only(self):
        service=HomeService(AppState());service.document=save_document(self.doc)
        device=service.document['devices'][2]
        worker=ConnectionWorker(service,device,[device],service.document['revision'])
        child=Mock();child.set_value.return_value={};child.status.return_value={'dps':{'1':False,'109':54,'111':True}}
        worker.children={device['id']:child};service.device_workers={device['id']:worker}
        service.report(device['id'],{'power':True,'tank_full':True})
        with self.assertRaises(ValueError):service.command(device['id'],True)
        service.command(device['id'],False);worker.drain_commands()
        child.set_value.assert_called_once_with(1,False)
        self.assertFalse(service.snapshot()[device['id']]['power'])
        service.report(device['id'],{'tank_full':False})
        child.status.return_value={'dps':{'1':True}}
        service.command(device['id'],True);worker.drain_commands()
        self.assertTrue(service.snapshot()[device['id']]['power'])
        self.assertIsNone(service.snapshot()[device['id']]['command_error'])

    def configure_legacy(self):
        update_settings(dict(dehum_device_id='tuya-2',dehum_ip='192.168.1.50',dehum_local_key='0123456789abcdef',dehum_auto_enabled=True))

    def test_shared_dashboard_readings_and_manual_controls_use_home_owner(self):
        self.configure_legacy()
        app,state,*_=create_app();saved=save_document(self.doc)
        service=app.extensions['home_service'];service.document=saved
        service.report('device-2',dict(power=False,humidity_pct=54,target_humidity_pct=50,tank_full=False))
        self.assertEqual(state.get('dehum_managed_by_home'),'device-2')
        self.assertEqual(state.get('dehum_humidity'),54)
        self.assertIs(state.get('dehum_power'),False)
        service.command=Mock()
        with patch('app.web.routes.Dehumidifier.from_settings') as direct:
            response=app.test_client().post('/api/dehum/power',json={'on':True})
            self.assertEqual(response.status_code,202)
            service.command.assert_called_once_with('device-2',True,origin='Dashboard manual control')
            self.assertEqual(app.test_client().get('/api/dehum/test').json['managed_by'],'Home')
            direct.assert_not_called()
        service.connection_status('device-2','offline')
        self.assertIsNone(state.get('dehum_power'))
        self.assertTrue(state.get('dehum_error'))

    def test_legacy_auto_controller_skips_only_the_managed_device(self):
        self.configure_legacy();save_document(self.doc)
        fleet=MinerFleet();state=AppState()
        control=ControlLoop(state,fleet,Mock(),Mock())
        with patch('app.control.loop.run_auto_control') as auto, patch('app.control.loop.os.getenv', return_value=''):
            control.run_cycle()
            auto.assert_not_called()
            doc=load_document();doc['devices'][2]['enabled']=False;doc['rules'][0]['enabled']=False;save_document(doc)
            control.run_cycle()
            auto.assert_called_once()
        changed=copy.deepcopy(self.doc);changed['devices'][2]['device_id']='another';changed['devices'][2]['address']='192.168.1.60'
        self.assertIsNone(legacy_owner(changed,dict(dehum_device_id='tuya-2',dehum_ip='192.168.1.50')))

    def test_discovery_classifies_dehumidifier_and_preserves_mappings(self):
        relations=[dict(dpId=1,statusCode='switch',valueType='Boolean',supportLocal=True),
            dict(dpId=109,statusCode='humidity_indoor',valueType='Integer',supportLocal=True,valueDesc='{"scale":0,"unit":"%"}'),
            dict(dpId=3,statusCode='humidity_set',valueType='Integer',supportLocal=True,valueDesc='{"scale":0,"unit":"%"}'),
            dict(dpId=111,statusCode='water_full',valueType='Boolean',supportLocal=True)]
        raw=dict(id='dehum-new',name='Ivation',category='cs',ip='192.168.1.50',local_key='0123456789abcdef',relations=relations)
        kind,points=mapping(raw)
        self.assertEqual(kind,'dehumidifier');self.assertEqual(points['humidity_dp'],'109');self.assertEqual(points['tank_full_dp'],'111')
        service=DiscoveryService(Mock(),AppState())
        service.candidates=catalog([raw],dict(matches={'dehum-new':dict(address='192.168.1.50',version=3.4)},reachable=['192.168.1.50']),load_document())
        service.batch='batch';service.expires=time.time()+60
        saved,count,disabled=service.import_devices(dict(batch='batch',revision=0,items=[dict(id='dehum-new')]))
        self.assertEqual((count,disabled),(1,0));self.assertEqual(saved['devices'][0]['target_humidity_dp'],'3')
        self.assertEqual(saved['devices'][0]['humidity_dp'],'109')
        raw['relations']=relations[:1]
        kind,points=mapping(raw)
        self.assertEqual(kind,'dehumidifier');self.assertEqual(points['humidity_dp'],'');self.assertEqual(points['tank_full_dp'],'')
        raw['relations']=[]
        self.assertIsNone(mapping(raw)[0])


if __name__=='__main__':unittest.main()
