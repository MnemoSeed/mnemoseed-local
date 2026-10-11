"""The session's loopback port allocation helpers."""

from __future__ import annotations

import socket

from _ports import LOOPBACK_HOST, RESERVED_PORTS, listening_loopback_socket, unused_loopback_port


def test_an_unused_loopback_port_is_not_reserved_and_can_be_bound() -> None:
    port = unused_loopback_port()

    assert port not in RESERVED_PORTS
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK_HOST, port))


def test_a_listening_loopback_socket_stays_bound_and_accepts_a_dial() -> None:
    with listening_loopback_socket() as (listener, port):
        assert port not in RESERVED_PORTS
        assert listener.getsockname()[:2] == (LOOPBACK_HOST, port)
        with socket.create_connection((LOOPBACK_HOST, port), timeout=5.0):
            pass
        assert listener.getsockname()[1] == port, "the listener must stay bound for the block"


def test_a_listening_loopback_socket_reports_a_different_port_each_time() -> None:
    with listening_loopback_socket() as (_, first):
        pass
    with listening_loopback_socket() as (_, second):
        pass

    assert first != second
