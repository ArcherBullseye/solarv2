import unittest
from unittest.mock import Mock, patch

from app.clients.zigbee import ZigbeeListener
from app.config import DEFAULTS
from app.control.home_triggers import TemperatureTrigger
from app.state import AppState


class HomeTriggerTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            **DEFAULTS,
            'home_hub_enabled': True,
            'home_temperature_alert_enabled': True,
            'home_temperature_alert_low_f': 50.0,
            'home_temperature_alert_high_f': 77.0,
            'home_sensor_name': 'Shed <outside>',
        }
        self.bot = Mock(chat_id='test-chat', token='test-token')
        bot_patch = patch('app.control.home_triggers.get_bot', return_value=self.bot)
        bot_patch.start()
        self.addCleanup(bot_patch.stop)
        state = AppState()
        self.trigger = TemperatureTrigger()
        self.listener = ZigbeeListener(state, lambda: self.settings, self.trigger.observe)

    def temperature(self, value):
        self.listener.accept({'dps': {'103': value}}, self.settings)

    def test_crossings_from_sensor_reports_and_rearm(self):
        self.temperature(10)  # 50 °F is inside the configured range.
        self.bot.send.assert_not_called()
        self.temperature(9)
        self.temperature(8)
        self.listener.accept({'dps': {'101': 55}}, self.settings)
        self.assertEqual(self.bot.send.call_count, 1)
        self.assertIn('48.2 °F (below 50 °F)', self.bot.send.call_args.args[0])
        self.assertIn('Shed &lt;outside&gt;', self.bot.send.call_args.args[0])

        self.temperature(25)  # 77 °F is also inside the range.
        self.temperature(26)
        self.assertEqual(self.bot.send.call_count, 2)
        self.assertIn('78.8 °F (above 77 °F)', self.bot.send.call_args.args[0])
        self.temperature(9)
        self.assertEqual(self.bot.send.call_count, 3)

    def test_startup_and_config_changes_do_not_spuriously_alert(self):
        self.temperature(30)
        self.settings['home_temperature_alert_low_f'] = 60.0
        self.temperature(10)
        self.settings['home_temperature_alert_enabled'] = False
        self.trigger.observe(self.settings)
        self.settings['home_temperature_alert_enabled'] = True
        self.temperature(30)
        self.bot.send.assert_not_called()
