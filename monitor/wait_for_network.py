"""Bounded wait for an assigned global address and matching default route."""

import argparse
import json
import subprocess
import time


def network_ready(command_timeout=5):
    for family in ('-4', '-6'):
        try:
            addresses = subprocess.run(['ip', '-j', family, 'address', 'show', 'scope', 'global'],
                                       capture_output=True, text=True, check=True, timeout=command_timeout)
            routes = subprocess.run(['ip', '-j', family, 'route', 'show', 'default'],
                                    capture_output=True, text=True, check=True, timeout=command_timeout)
            interfaces = {
                interface['ifname'] for interface in json.loads(addresses.stdout)
                if interface.get('operstate') != 'DOWN' and
                any(info.get('scope') == 'global' and info.get('local') and
                       not info.get('tentative') and not info.get('dadfailed') and
                       'tentative' not in info.get('flags', []) and 'dadfailed' not in info.get('flags', [])
                       for info in interface.get('addr_info', []))
            }
            if any(route.get('dev') in interfaces and 'linkdown' not in route.get('flags', [])
                   for route in json.loads(routes.stdout)):
                return True
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            continue
    return False


def wait_for_network(timeout=120, interval=5):
    deadline = time.monotonic() + timeout
    while True:
        remaining = max(1.0, deadline - time.monotonic())
        if network_ready(command_timeout=min(3, remaining / 4)):
            print('Network address and default route are ready.', flush=True)
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print('Network is not ready: no usable address/default route before the DHCP wait expired.', flush=True)
            return False
        print(f'Waiting for DHCP/network readiness ({int(remaining)} seconds remaining)...', flush=True)
        time.sleep(min(interval, remaining))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=float, default=120)
    args = parser.parse_args()
    if args.timeout < 0:
        parser.error('timeout must not be negative')
    return 0 if wait_for_network(args.timeout) else 1


if __name__ == '__main__':
    raise SystemExit(main())
