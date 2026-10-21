#!/usr/bin/env python3
"""Only temporary fixture state; stdout protocol, never opens sockets."""
import json
import os
import pathlib
import signal
import sys
import time

root = pathlib.Path(os.environ['KANPILOT_MOBILE_FIXTURE_ROOT'])
state_path = root / 'state.json'
state = json.loads(state_path.read_text()) if state_path.exists() else {'enabled': False}
command = sys.argv[1]
payload = json.loads(sys.argv[3])

def status():
    addresses = [{'address': '127.0.0.1', 'name': 'fixture'}]
    if os.environ.get('KANPILOT_MOBILE_FIXTURE_LAN_FIRST') == '1':
        addresses.insert(0, {'address': '10.0.0.1', 'name': 'CI LAN example'})
    return dict(api_version=1, server_id='fixture', enabled=state['enabled'], bind_host='127.0.0.1',
                port=47831, endpoint='https://127.0.0.1:47831', certificate_sha256=None,
                addresses=addresses, devices=[])

if command == 'mobile-status':
    print(json.dumps(status()))
elif command == 'set-mobile-access':
    if payload.get('enabled') and payload.get('bind_host') != '127.0.0.1':
        print(json.dumps({'error': 'fixture must bind loopback, not a LAN interface', 'code': 'bad_request'}))
        sys.exit(1)
    state['enabled'] = payload['enabled']
    state_path.write_text(json.dumps(state))
    print(json.dumps(status()))
elif command == 'mobile-serve':
    if os.environ.get('KANPILOT_MOBILE_FIXTURE_MODE') == 'failure':
        print(json.dumps({'error': '手机连接端口已被占用', 'code': 'unavailable'}), flush=True)
        sys.exit(1)
    time.sleep(.3)
    print(json.dumps({'ready': True, 'endpoint': status()['endpoint']}), flush=True)
    signal.signal(signal.SIGTERM, lambda *args: sys.exit(0))
    (root / 'service-started').write_text(str(os.getpid()))
    while True:
        time.sleep(.1)
else:
    raise AssertionError(command)
