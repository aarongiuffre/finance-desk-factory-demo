"""Loopback sockets must still work under the offline guard (the future
search_server HTTP test depends on this): connect for real on 127.0.0.1,
exchange one byte, and confirm external connects are refused loudly."""

import socket
import threading

import pytest


def test_loopback_connect_send_recv_roundtrip():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.settimeout(5)
    srv.bind(("127.0.0.1", 0))          # ephemeral port: never touches live services
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        try:
            conn, _ = srv.accept()
            conn.settimeout(5)
            conn.sendall(b"A")
            conn.close()
        except OSError:
            pass

    th = threading.Thread(target=serve, daemon=True)
    th.start()

    cli = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    cli.settimeout(5)
    cli.connect(("127.0.0.1", port))     # must be a REAL connect, not a no-op
    assert cli.recv(1) == b"A"
    cli.close()
    th.join(timeout=5)
    srv.close()


def test_external_connect_still_raises():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(AssertionError, match="forbidden"):
        s.connect(("example.com", 443))
    with pytest.raises(AssertionError, match="forbidden"):
        s.connect_ex(("192.0.2.1", 80))  # TEST-NET-1: never routable anyway
