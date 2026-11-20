import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
from app.db import init_db
from app.home.engine import RuleEngine
from app.home.groups import execution_document, execution_rule
from app.home.runtime import HomeService
from app.home.store import init_home_store, load_document, save_document
from app.state import AppState
from test_home_workspace import fixture


class AutomationGroupTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        p=patch('app.db.DB_PATH',Path(temp.name)/'controller.db');p.start();self.addCleanup(p.stop)
        init_db();init_home_store()

    def grouped(self):
        doc=fixture()
        doc['groups']=[dict(id='solar-loads',name='Solar loads',icon='solar')]
        doc['rules'][0]['group_id']='solar-loads'
        return doc

    def test_groups_and_assignment_roundtrip_rename_preserves_rule_behavior(self):
        saved=save_document(self.grouped())
        before=execution_document(saved)
        saved['groups'][0].update(name='Room heaters',icon='heating')
        renamed=save_document(saved)
        self.assertEqual(renamed['groups'][0],dict(id='solar-loads',name='Room heaters',icon='heating'))
        self.assertEqual(renamed['rules'][0]['group_id'],'solar-loads')
        self.assertEqual(execution_document(renamed),before)
        self.assertEqual(renamed['hubs'][0]['local_key'],'1234567890abcdef')

    def test_old_documents_stay_ungrouped_with_same_rule_objects(self):
        saved=save_document(fixture())
        self.assertEqual(saved['groups'],[])
        self.assertEqual(saved['rules'],fixture()['rules'])
        saved['groups']=[dict(id='empty',name='Ready for later',icon='folder')]
        result=save_document(saved)
        self.assertEqual(result['rules'],fixture()['rules'])

    def test_invalid_group_names_icons_ids_and_references_are_atomic(self):
        saved=save_document(self.grouped())
        for issue in ('blank','long-name','icon','duplicate','unknown','bad-id','too-many'):
            bad=copy.deepcopy(saved)
            if issue=='blank':bad['groups'][0]['name']='   '
            if issue=='long-name':bad['groups'][0]['name']='x'*81
            if issue=='icon':bad['groups'][0]['icon']='<script>'
            if issue=='duplicate':bad['groups'].append(copy.deepcopy(bad['groups'][0]))
            if issue=='unknown':bad['rules'][0]['group_id']='missing'
            if issue=='bad-id':bad['groups'][0]['id']='invalid id'
            if issue=='too-many':bad['groups']=[dict(id='g'+str(i),name='Group',icon='folder') for i in range(51)]
            with self.subTest(issue=issue),self.assertRaises(ValueError):save_document(bad)
            self.assertEqual(load_document(),saved)

    def test_removing_a_group_retains_automations_as_ungrouped(self):
        saved=save_document(self.grouped())
        behavior=execution_rule(saved['rules'][0])
        saved['groups']=[]
        with self.assertRaises(ValueError):save_document(saved)
        saved['rules'][0]['group_id']=''
        ungrouped=save_document(saved)
        self.assertEqual(ungrouped['groups'],[])
        self.assertEqual(ungrouped['rules'][0],behavior)
        self.assertTrue(ungrouped['rules'][0]['enabled'])

    def test_older_callers_preserve_group_metadata_when_fields_are_omitted(self):
        saved=save_document(self.grouped())
        expected=copy.deepcopy(saved['groups'])
        saved.pop('groups');saved['rules'][0].pop('group_id')
        result=save_document(saved)
        self.assertEqual(result['groups'],expected)
        self.assertEqual(result['rules'][0]['group_id'],'solar-loads')

    def test_group_changes_keep_connections_and_crossing_cooldown_memory(self):
        doc=fixture();doc['rules'][0]['conditions'][0]['value']=50.0
        doc['rules'][0]['for_seconds']=60
        save_document(doc)
        service=HomeService(AppState())
        with patch('app.home.runtime.ConnectionWorker.start'):
            service.tick()
            service.report('device-0',dict(temperature_f=60))
            service.tick()
            service.report('device-0',dict(temperature_f=40))
            service.tick()
            engine=service.engine;workers=list(service.workers)
            memory=copy.deepcopy(engine.baselines['rule'])
            changed=load_document()
            changed['groups']=[dict(id='climate',name='Climate',icon='temperature')]
            changed['rules'][0]['group_id']='climate'
            changed['rules'][0]['conditions'][0]['value']=50 # browser JSON roundtrip
            saved=save_document(changed)
            service.tick()
        self.assertIs(service.engine,engine)
        self.assertEqual(service.workers,workers)
        self.assertFalse(any(worker.stop_event.is_set() for worker in workers))
        self.assertEqual(engine.baselines['rule'],memory)
        self.assertEqual(workers[0].revision,saved['revision'])

    def test_engine_signature_ignores_group_assignments_but_tracks_real_changes(self):
        first=fixture()['rules'][0]
        grouped=copy.deepcopy(first);grouped['group_id']='some-group'
        self.assertEqual(execution_rule(first),execution_rule(grouped))
        grouped['conditions'][0]['value']=55
        self.assertNotEqual(execution_rule(first),execution_rule(grouped))

    def test_api_roundtrip_exposes_icon_choices_and_keeps_credentials_masked(self):
        app,*_=create_app();client=app.test_client()
        response=client.post('/api/home',json=self.grouped())
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['document']['groups'][0]['name'],'Solar loads')
        payload=client.get('/api/home').json
        self.assertEqual(payload['group_icons']['solar']['symbol'],'☀️')
        self.assertEqual(payload['document']['hubs'][0]['local_key'],'••••')
        self.assertEqual(payload['document']['rules'][0]['group_id'],'solar-loads')


if __name__=='__main__':unittest.main()
