"""Test runner's external network deny guard, inherited by Python children.

Loopback is reserved for isolated synthetic HTTP servers. The runner removes
proxy and credential configuration; existing tests may install stronger guards.
Audit hooks cannot be undone by application monkeypatching socket functions.
"""
import ipaddress
import os
import socket
import sys


def _loopback(host):
    if host in ("localhost", b"localhost"):
        return True
    try:
        return ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host).is_loopback
    except (ValueError, TypeError):
        return False


def _deny(event):
    log = os.environ.get("ATLAS_OFFLINE_DENIAL_LOG")
    if log:
        # Log event type only: no credentials, headers, URLs or source bytes.
        with open(log, "a", encoding="utf-8") as stream:
            stream.write(event + "\n")
    raise RuntimeError("external transport forbidden by offline test runner: " + event)


def _guard(event, args):
    if event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex"):
        if not _loopback(args[0]):
            _deny(event)
    elif event in ("socket.connect", "socket.bind", "socket.sendto"):
        sock = args[0]
        if sock.family == socket.AF_UNIX:
            return
        address = args[-1]
        if type(address) is not tuple or not address or not _loopback(address[0]):
            _deny(event)


sys.addaudithook(_guard)
