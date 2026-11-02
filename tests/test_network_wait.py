"""No external network calls: simulate address assignment, routes and time."""

import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('network_wait_test', ROOT / 'monitor/wait_for_network.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def result(value):
    return mock.Mock(stdout=json.dumps(value))


class NetworkWaitTests(unittest.TestCase):
    def addresses(self, **changes):
        interface = dict(ifname='eth0', operstate='UP', addr_info=[dict(local='192.0.2.10', scope='global')])
        interface.update(changes)
        return [interface]

    def test_requires_address_and_route_on_same_interface(self):
        for route, ready in [([dict(dev='eth0')], True), ([], False), ([dict(dev='eth1')], False)]:
            with self.subTest(route=route), mock.patch.object(MODULE.subprocess, 'run', side_effect=[
                    result(self.addresses()), result(route), result([]), result([])]):
                self.assertEqual(MODULE.network_ready(), ready)

    def test_supports_ipv6_when_ipv4_is_not_yet_ready(self):
        with mock.patch.object(MODULE.subprocess, 'run', side_effect=[result([]), result([]),
                result(self.addresses(addr_info=[dict(local='2001:db8::2', scope='global')])), result([dict(dev='eth0')])]):
            self.assertTrue(MODULE.network_ready())

    def test_rejects_link_local_tentative_or_link_down_addresses(self):
        addresses = [self.addresses(operstate='DOWN'),
                     self.addresses(addr_info=[dict(local='fe80::1', scope='link')]),
                     self.addresses(addr_info=[dict(local='2001:db8::2', scope='global', tentative=True)])]
        for address in addresses:
            with self.subTest(address=address), mock.patch.object(MODULE.subprocess, 'run', side_effect=[
                    result(address), result([dict(dev='eth0')]), result([]), result([])]):
                self.assertFalse(MODULE.network_ready())

    def test_command_errors_are_not_mistaken_for_readiness(self):
        for error in [OSError('ip unavailable'), subprocess.TimeoutExpired('ip', 1)]:
            with self.subTest(error=error), mock.patch.object(MODULE.subprocess, 'run', side_effect=error):
                self.assertFalse(MODULE.network_ready())

    def test_bounded_wait_recovers_if_dhcp_finishes_on_next_check(self):
        with mock.patch.object(MODULE, 'network_ready', side_effect=[False, True]), \
                mock.patch.object(MODULE.time, 'sleep') as sleep:
            self.assertTrue(MODULE.wait_for_network(timeout=120))
        sleep.assert_called_once()

    def test_no_network_exits_after_timeout_without_sleeping_forever(self):
        with mock.patch.object(MODULE, 'network_ready', return_value=False), \
                mock.patch.object(MODULE.time, 'monotonic', side_effect=[0, 0, 121]), \
                mock.patch.object(MODULE.time, 'sleep') as sleep:
            self.assertFalse(MODULE.wait_for_network(timeout=120))
        sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
