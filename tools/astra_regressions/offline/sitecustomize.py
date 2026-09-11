"""Independent audit isolation. No external DNS/connect/sendto in Python.

Test subprocesses inherit this module through PYTHONPATH. Proxy settings and
real credentials are excluded by run_isolated.py. Loopback is allowed only
for the original suite's local dashboard server, never in adversarial probes.
"""
import os
import sys

def _audit(event, args):
    if event not in ('socket.connect', 'socket.getaddrinfo', 'socket.sendto'):
        return
    if event == 'socket.getaddrinfo':
        host = args[0]
    else:
        address = args[1] if event == 'socket.connect' else args[-1]
        host = address[0] if isinstance(address, tuple) else address
    local = host in ('127.0.0.1', '::1', 'localhost', b'127.0.0.1', b'localhost')
    if local and os.environ.get('ATLAS_AUDIT_ALLOW_LOOPBACK') == '1':
        return
    raise RuntimeError('ATLAS_AUDIT_NETWORK_DENIED: ' + event)

sys.addaudithook(_audit)
