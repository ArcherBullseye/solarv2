import copy
import json
import queue
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import create_app
from app.db import init_db
from app.home.discovery import DiscoveryService, catalog
from app.home.metrics import definitions
from app.home.runtime import HomeService, ConnectionWorker, decoded_values
from app.home.store import init_home_store, load_document, save_document, ConflictError
from app.home.telemetry import schema_readings, validate_telemetry
from app.state import AppState
from test_home_workspace import fixture


def relation(dp,code,unit,scale=0,kind='Integer'):
    return dict(dpId=dp,statusCode=code,valueType=kind,supportLocal=True,valueConvert='default',
                valueDesc=json.dumps(dict(unit=unit,scale=scale)))


def schema():
    return dict(relations=[relation(19,'cur_power','W',1),relation(18,'cur_current','mA'),
        relation(20,'cur_voltage','V',1),relation(17,'add_ele','kWh',3),
        relation(30,'total_energy','Wh'),relation(31,'compressor_state','',kind='Boolean'),
        dict(dpId=6,statusCode='mode',valueType='Enum',supportLocal=True,valueDesc='{"range":["normal","continuous"]}'),
        dict(dpId=100,statusCode='wifi_password',valueType='String',supportLocal=True)])


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        p=patch('app.db.DB_PATH',Path(temp.name)/'controller.db');p.start();self.addCleanup(p.stop)
        init_db();init_home_store()
        self.metrics=schema_readings(schema())

    def test_schema_unit_scale_conversion_and_increment_is_not_total(self):
        device=dict(kind='plug',power_dp='1',telemetry=self.metrics)
        values=decoded_values({'1':True,'19':5255,'18':4380,'20':1201,'17':125,'30':2500,'31':True,'6':'normal'},device)
        self.assertEqual(values['power_w'],525.5)
        self.assertEqual(values['current_a'],4.38)
        self.assertEqual(values['voltage_v'],120.1)
        self.assertEqual(values['energy_increment_kwh'],.125)
        self.assertEqual(values['energy_kwh'],2.5)
        self.assertTrue(values['extra_compressor_state']);self.assertEqual(values['extra_mode'],'normal')
        self.assertNotIn('extra_wifi_password',values)
        self.assertEqual(len(self.metrics),7)

    def test_missing_units_scales_unsupported_transform_and_nan_do_not_guess(self):
        for changed in ('unit','scale','transform','local'):
            raw=schema()
            if changed=='unit':raw['relations'][0]['valueDesc']='{"scale":1}'
            if changed=='scale':raw['relations'][0]['valueDesc']='{"unit":"W"}'
            if changed=='transform':raw['relations'][0]['valueConvert']='custom-conversion'
            if changed=='local':raw['relations'][0]['supportLocal']=False
            self.assertNotIn('power_w',[item['id'] for item in schema_readings(raw)])
        device=dict(kind='plug',telemetry=self.metrics)
        for raw in (True,'100',float('nan'),float('inf'),None):
            self.assertNotIn('power_w',decoded_values({'19':raw},device))
        self.assertEqual(decoded_values({'19':0,'18':0},device),{'power_w':0,'current_a':0})
        self.assertNotIn('power_w',decoded_values({},device))
        self.assertNotIn('extra_mode',decoded_values({'6':'unknown-private-value'},device))

    def test_mapping_validation_preserves_manual_scale_and_rejects_core_overrides(self):
        self.assertEqual(validate_telemetry(self.metrics),self.metrics)
        for mutation in ('duplicate','core','zero','nan','wrong-unit','invalid-dp','raw-text'):
            candidate=copy.deepcopy(self.metrics)
            if mutation=='duplicate':candidate.append(candidate[0])
            if mutation=='core':candidate[0]['id']='power'
            if mutation=='zero':candidate[0]['divisor']=0
            if mutation=='nan':candidate[0]['divisor']=float('nan')
            if mutation=='wrong-unit':candidate[0]['unit']='mA'
            if mutation=='invalid-dp':candidate[0]['dp']='0'
            if mutation=='raw-text':candidate[0]['kind']='string'
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):validate_telemetry(candidate)
        self.assertEqual(len(schema_readings(schema(),used_dps=['19'])),6)

    def test_readings_roundtrip_and_become_numeric_boolean_conditions(self):
        doc=fixture();doc['devices'][2]['telemetry']=self.metrics
        doc['rules'][0]['conditions']=[dict(id='watts',source='device-2',metric='power_w',operator='above',value=400),
            dict(id='compressor',source='device-2',metric='extra_compressor_state',operator='is_on')]
        saved=save_document(doc)
        self.assertEqual(saved['devices'][2]['telemetry'],self.metrics)
        self.assertIn('power_w',definitions('device-2',saved['devices'][2]))
        self.assertNotIn('extra_mode',definitions('device-2',saved['devices'][2]))

    def test_readonly_info_retains_numeric_reports_and_filters_private_values(self):
        app,state,*_=create_app();saved=save_document(fixture())
        service=app.extensions['home_service'];service.document=saved
        service.report_points('device-2',{'19':5255,'18':4380,'1':False,'100':'private-password','101':{'key':'private-key'},'102':float('nan')})
        client=app.test_client();data=client.get('/api/home/devices/device-2/info').json
        self.assertEqual({row['dp'] for row in data['raw']},{'1','18','19'})
        self.assertIs(data['raw'][0]['value'],False)
        self.assertNotIn('private',json.dumps(data));self.assertNotIn('local_key',json.dumps(data))
        service.report_points('device-2',{str(i):i for i in range(1,200)})
        self.assertLessEqual(len(service.device_info('device-2')['raw']),128)
        self.assertEqual(client.get('/api/home/devices/missing/info').status_code,404)

    def test_refresh_only_reads_through_existing_worker_and_does_not_switch(self):
        doc=fixture();doc['devices'][2]['telemetry']=self.metrics
        service=HomeService(AppState());service.document=save_document(doc)
        device=service.document['devices'][2]
        worker=ConnectionWorker(service,service.document['hubs'][0],[device],service.document['revision'])
        child=Mock();child.status.return_value={'dps':{'1':False,'19':5255,'18':4380}}
        worker.children={device['id']:child};service.device_workers={device['id']:worker}
        service.refresh_device(device['id'])
        with self.assertRaises(ValueError):service.refresh_device(device['id'])
        worker.read_requested_status()
        child.status.assert_called_once();child.set_value.assert_not_called()
        self.assertTrue(worker.commands.empty())
        self.assertEqual(service.snapshot()[device['id']]['power_w'],525.5)
        self.assertFalse(service.snapshot()[device['id']]['power'])

    def test_account_measurement_update_preserves_existing_manual_data_and_ids(self):
        doc=fixture()
        manual=dict(self.metrics[0],divisor=100,origin='manual')
        doc['devices'][2]['telemetry']=[manual]
        before=save_document(doc)
        raw=dict(schema(),id='tuya-2',name='Plug',category='cz')
        raw['relations'].append(relation(1,'switch_1','',kind='Boolean'))
        raw['relations'].append(relation(101,'battery_percentage','%'))
        service=DiscoveryService(Mock(),AppState())
        service.candidates=catalog([raw],dict(matches={},reachable=[]),before)
        service.batch='batch';service.expires=time.time()+60
        with self.assertRaises(ConflictError):service.update_measurements(dict(batch='batch',revision=0))
        saved,updated=service.update_measurements(dict(batch='batch',revision=before['revision']))
        self.assertEqual(updated,1)
        self.assertEqual(saved['devices'][2]['id'],before['devices'][2]['id'])
        self.assertEqual(saved['rules'],before['rules'])
        self.assertEqual(saved['hubs'],before['hubs'])
        after={item['id']:item for item in saved['devices'][2]['telemetry']}
        self.assertEqual(after['power_w'],manual)
        self.assertEqual(after['current_a']['divisor'],1000)
        self.assertEqual(after['battery_pct']['dp'],'101')
        for old,new in zip(before['devices'],saved['devices']):
            self.assertEqual({k:v for k,v in old.items() if k!='telemetry'},{k:v for k,v in new.items() if k!='telemetry'})


if __name__=='__main__':unittest.main()
