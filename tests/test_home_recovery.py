"""Power rules reconcile fresh matched conditions, including after recovery."""
import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from app.home.engine import RuleEngine
from test_home_workspace import fixture

NOW = 1795186800


def reading(metric, value, when):
    return {'status': 'connected', metric: value,
            'updated_at': {metric: datetime.fromtimestamp(when, timezone.utc).isoformat()}}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.doc = fixture()
        self.rule = self.doc['rules'][0]
        self.rule.update(name='Freezer high', for_seconds=0, cooldown_seconds=60)
        self.rule['conditions'][0].update(operator='above', value=40)
        self.engine, self.action, self.log = RuleEngine(), Mock(), Mock()

    def tick(self, elapsed=0, temp=46.4, power=False, **target):
        when = NOW + elapsed
        devices = {'device-0': reading('temperature_f', temp, when),
                   'device-2': dict(reading('power', power, when), **target)}
        self.engine.tick(self.doc, devices, {}, 'America/Detroit', self.action, self.log, when)
        return getattr(self.engine, 'states', {}).get('rule')

    def test_already_high_on_startup_turns_on(self):
        self.tick()
        self.assertEqual(self.action.call_count, 2)
        self.assertTrue(self.action.call_args_list[0].args[0]['on'])

    def test_duration_and_sensor_recovery_do_not_require_new_crossing(self):
        self.rule['for_seconds'] = 5
        self.assertEqual(self.tick()['code'], 'duration')
        self.tick(4)
        self.action.assert_not_called()
        self.tick(5)
        self.assertEqual(self.action.call_count, 2)
        # A stale condition pauses control and restarts the duration, but the
        # already-high reading after recovery must still eventually run.
        self.engine = RuleEngine()
        stale = {'device-0': reading('temperature_f', 46.4, NOW - 1801)}
        self.engine.tick(self.doc, stale, {}, 'America/Detroit', self.action, self.log, NOW)
        self.assertEqual(self.engine.states['rule']['code'], 'readings')
        self.action.reset_mock()
        self.tick(1)
        self.tick(5)
        self.action.assert_not_called()
        self.tick(6)
        self.assertEqual(self.action.call_count, 2)

    def test_correct_target_needs_no_commands_or_startup_notification(self):
        self.assertEqual(self.tick(power=True)['code'], 'satisfied')
        self.tick(100, power=True)
        self.action.assert_not_called()
        self.tick(101, power=False)
        self.assertEqual(self.action.call_count, 2)

    def test_manual_hold_and_pending_do_not_consume_match(self):
        self.assertEqual(self.tick(hold_until=NOW+900)['code'], 'manual_hold')
        self.assertEqual(self.tick(899, hold_until=NOW+900)['code'], 'manual_hold')
        self.action.assert_not_called()
        self.tick(900, hold_until=NOW+900)
        self.assertEqual(self.action.call_count, 2)
        self.assertEqual(self.tick(901, pending=True)['code'], 'pending')
        self.tick(1000, power=True)
        self.assertEqual(self.action.call_count, 2)

    def test_offline_stale_and_non_boolean_target_pause_control(self):
        for target in ({'status':'offline'}, {'power':0},
                       {'updated_at':{'power':datetime.fromtimestamp(NOW-1801, timezone.utc).isoformat()}}):
            with self.subTest(target=target):
                self.assertEqual(self.tick(**target)['code'], 'target_unavailable')
        self.action.assert_not_called()
        self.tick(1)
        self.assertEqual(self.action.call_count, 2)

    def test_failed_command_retries_and_successful_telegram_does_not_repeat(self):
        # Same action order as the user's rule: Telegram precedes power.
        self.rule['actions'].reverse()
        def action(a, name):
            if a['type'] == 'plug':
                raise ValueError('Connection busy. Try again shortly.')
        self.action.side_effect = action
        self.assertEqual(self.tick()['code'], 'retry')
        self.assertEqual(self.action.call_count, 2)
        self.assertEqual(self.tick(59)['code'], 'cooldown')
        self.tick(60)
        self.assertEqual(self.action.call_count, 3)
        self.action.side_effect = None
        self.tick(120)
        self.assertEqual(self.action.call_count, 4)
        self.tick(121, power=True)
        self.tick(180, power=True)
        self.assertEqual(self.action.call_count, 4)
        self.assertEqual(self.engine.states['rule']['code'], 'satisfied')

    def test_async_failure_retries_without_duplicate_messages_or_commands_while_pending(self):
        self.tick()
        self.tick(60, pending=True)
        self.assertEqual(self.action.call_count, 2)
        self.tick(61, command_error='Device did not confirm requested state.')
        self.assertEqual(self.action.call_count, 3)
        self.assertEqual(self.action.call_args.args[0]['type'], 'plug')

    def test_new_crossing_during_cooldown_runs_when_cooldown_ends(self):
        self.tick()
        self.tick(10, temp=30)
        self.assertEqual(self.tick(11)['code'], 'cooldown')
        self.tick(60)
        self.assertEqual(self.action.call_count, 4)

    def test_zero_cooldown_still_limits_retries_to_thirty_seconds(self):
        self.rule['cooldown_seconds'] = 0
        self.tick()
        self.tick(29)
        self.assertEqual(self.action.call_count, 2)
        self.tick(30)
        self.assertEqual(self.action.call_count, 3)

    def test_disabled_rule_never_runs(self):
        self.rule['enabled'] = False
        self.assertEqual(self.tick()['code'], 'paused')
        self.action.assert_not_called()
        self.rule['enabled'] = True
        self.tick(1)
        self.assertEqual(self.action.call_count, 2)

    def test_default_notification_only_rules_still_wait_for_crossing(self):
        self.rule['actions'] = [self.rule['actions'][1]]
        self.tick()
        self.tick(1)
        self.action.assert_not_called()
        self.tick(2, temp=30)
        self.tick(3)
        self.assertEqual(self.action.call_count, 1)
        self.tick(100)
        self.assertEqual(self.action.call_count, 1)

    def test_failed_telegram_retries_without_reswitching_confirmed_power(self):
        def action(a, name):
            if a['type'] == 'telegram':
                raise ValueError('Telegram unavailable.')
        self.action.side_effect = action
        self.tick()
        self.assertEqual(self.action.call_count, 2)
        self.action.side_effect = None
        self.tick(60, power=True)
        self.assertEqual(self.action.call_count, 3)
        self.assertEqual(self.action.call_args.args[0]['type'], 'telegram')
        self.assertEqual(self.tick(61, power=True)['code'], 'satisfied')

    def test_multiple_targets_skip_correct_and_wait_for_all_pending(self):
        self.rule['actions'].insert(0, dict(id='extra', type='plug', device_id='device-3', on=False))
        when = NOW
        devices = {'device-0':reading('temperature_f',46.4,when),
                   'device-2':reading('power',False,when),
                   'device-3':reading('power',False,when)}
        self.engine.tick(self.doc,devices,{},'America/Detroit',self.action,self.log,when)
        self.assertEqual(self.action.call_count,2)
        self.assertEqual(self.action.call_args_list[0].args[0]['device_id'],'device-2')

    def test_startup_off_rule_turns_off_an_on_target(self):
        self.rule['actions'][0]['on'] = False
        self.tick(power=True)
        self.assertEqual(self.action.call_count, 2)
        self.assertFalse(self.action.call_args_list[0].args[0]['on'])


class RecoveryApiTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from app import create_app
        from app.home.store import save_document
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = patch('app.db.DB_PATH', Path(temp.name)/'controller.db')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app, *_ = create_app()
        self.client = self.app.test_client()
        self.service = self.app.extensions['home_service']
        self.doc = save_document(fixture())
        self.service.document = self.doc

    def test_api_exposes_live_waiting_reason_and_preview_does_not_actuate(self):
        self.service.engine.tick(self.doc, {}, {}, 'America/Detroit', Mock(), Mock(), NOW)
        payload = self.client.get('/api/home').json
        self.assertEqual(payload['rule_states']['rule']['code'], 'readings')
        with unittest.mock.patch.object(self.service, 'action') as action:
            result = self.client.get('/api/home/preview/rule').json
            self.assertTrue(result['enabled'])
            self.assertEqual(result['status']['code'], 'readings')
            self.assertIsNone(result['result'])
            action.assert_not_called()
        # Saving a changed rule must not expose the previous revision's status.
        doc = copy.deepcopy(self.doc)
        doc['rules'][0]['enabled'] = False
        self.assertEqual(self.client.post('/api/home',json=doc).status_code,200)
        self.assertEqual(self.client.get('/api/home').json['rule_states'], {})
        result = self.client.get('/api/home/preview/rule').json
        self.assertFalse(result['enabled'])
        self.assertIsNone(result['status'])

    def test_run_behavior_validates_roundtrips_and_survives_older_clients(self):
        from app.home.store import save_document, load_document
        for mode in ('crossing', 'maintain'):
            doc = load_document()
            doc['rules'][0]['run_mode'] = mode
            saved = save_document(doc)
            self.assertEqual(saved['rules'][0]['run_mode'], mode)
            saved['rules'][0].pop('run_mode')
            self.assertEqual(save_document(saved)['rules'][0]['run_mode'], mode)
        for invalid in ('always', True, [], 1):
            doc = load_document()
            doc['rules'][0]['run_mode'] = invalid
            with self.subTest(mode=invalid), self.assertRaises(ValueError):
                save_document(doc)

    def test_real_queue_adapter_recovers_after_failed_device_confirmation(self):
        import time
        from app.home.runtime import ConnectionWorker
        from app.home.store import save_document
        doc = copy.deepcopy(self.doc)
        doc['rules'][0].update(for_seconds=0)
        doc['rules'][0]['conditions'][0].update(operator='above',value=40)
        doc['rules'][0]['actions'] = [doc['rules'][0]['actions'][0]]
        self.service.document = save_document(doc)
        self.service.report('device-0',{'temperature_f':46.4})
        self.service.child_availability('device-2', True)
        self.service.report('device-2',{'power':False})
        worker = ConnectionWorker(self.service,doc['hubs'][0],doc['devices'],self.service.document['revision'])
        child = Mock()
        child.set_value.return_value = {'dps':{'1':False},'cid':'node-2'}
        child.status.return_value = {'dps':{'1':False},'cid':'node-2'}
        worker.children = {'device-2':child}
        self.service.device_workers = {'device-2':worker}
        def tick(when):
            self.service.engine.tick(self.service.document,self.service.snapshot(),{},
                'America/Detroit',self.service.action,Mock(),when)
        now = time.time()
        tick(now)
        self.assertTrue(self.service.snapshot()['device-2']['pending'])
        worker.drain_commands()
        self.assertIsNotNone(self.service.snapshot()['device-2']['command_error'])
        tick(now+59)
        self.assertFalse(self.service.snapshot()['device-2']['pending'])
        tick(now+60)
        self.assertTrue(self.service.snapshot()['device-2']['pending'])
        child.status.return_value = {'dps':{'1':True},'cid':'node-2'}
        worker.drain_commands()
        tick(now+61)
        self.assertEqual(self.service.engine.states['rule']['code'],'satisfied')
        self.assertTrue(self.service.snapshot()['device-2']['power'])
        self.assertEqual(child.set_value.call_count,2)
