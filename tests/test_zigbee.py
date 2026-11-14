import unittest
from app.clients.zigbee import ZigbeeListener, decode_readings, is_sensor_report
from app.state import AppState
from app.config import DEFAULTS, mask_secrets, strip_masked

SETTINGS = {'home_temperature_dp': '103', 'home_humidity_dp': '101',
            'home_temperature_divisor': 10, 'home_humidity_divisor': 1,
            'home_temperature_unit': 'C'}

class ZigbeeTests(unittest.TestCase):
    def test_th02_defaults_decode_captured_report(self):
        self.assertEqual(decode_readings({"101": 96, "102": 100, "103": 11}, DEFAULTS),
                         {"humidity_pct": 96.0, "temperature_c": 11.0})

    def test_no_unverified_mapping(self):
        self.assertEqual(decode_readings({'101': 60, '103': 6}, {}), {})

    def test_partial_reports_preserve_other_reading_and_timestamp(self):
        state = AppState()
        listener = ZigbeeListener(state, lambda: SETTINGS)
        listener.accept({'dps': {'103': 215, '101': 60}}, SETTINGS)
        stamp = state.get('home_sensor')['updated_at']['temperature_c']
        listener.accept({'data': {'dps': {'101': 62}}}, SETTINGS)
        sensor = state.get('home_sensor')
        self.assertEqual(sensor['temperature_c'], 21.5)
        self.assertEqual(sensor['humidity_pct'], 62)
        self.assertEqual(sensor['updated_at']['temperature_c'], stamp)
        listener.publish(SETTINGS, 'offline', 'Hub unavailable')
        self.assertEqual(state.get('home_sensor')['temperature_c'], 21.5)
        self.assertFalse(listener.accept(None, SETTINGS))

    def test_filter_child_and_nested_node(self):
        child = object()
        self.assertTrue(is_sensor_report({'data': {'cid': 'our-node'}}, child, 'our-node'))
        self.assertTrue(is_sensor_report({'device': child}, child, 'our-node'))
        self.assertFalse(is_sensor_report({'cid': 'another-node', 'dps': {'101': 99}}, child, 'our-node'))
        self.assertFalse(is_sensor_report(None, child, 'our-node'))

    def test_invalid_and_zero_values(self):
        for value in [None, True, 'bad', float('nan'), float('inf'), 101, -1]:
            self.assertNotIn('humidity_pct', decode_readings({'101': value}, SETTINGS))
        self.assertEqual(decode_readings({'101': 0, '103': -50}, SETTINGS),
                         {'humidity_pct': 0.0, 'temperature_c': -5.0})
        self.assertEqual(decode_readings({'103': 680}, {**SETTINGS, 'home_temperature_unit': 'F'}),
                         {'temperature_c': 20.0})
        self.assertEqual(decode_readings({'103': 200}, {**SETTINGS, 'home_temperature_divisor': 0}), {})

    def test_key_masked_and_refresh_wakes_listener(self):
        masked = mask_secrets({'home_hub_key': 'private-test-key'})
        self.assertNotEqual(masked['home_hub_key'], 'private-test-key')
        self.assertEqual(strip_masked(masked), {})
        state = AppState(); state.refresh_all()
        self.assertTrue(state.home_refresh.is_set())

if __name__ == '__main__': unittest.main()
