"""Outbound IPv4 pinning for the Zerodha order path."""

from __future__ import annotations

import socket

import pytest

from src import net


@pytest.fixture(autouse=True)
def _restore():
    net.reset()
    yield
    net.reset()


def test_prefer_ipv4_is_idempotent():
    assert net.prefer_ipv4() is True
    assert net.prefer_ipv4() is False


def test_unnamed_family_is_forced_to_ipv4(monkeypatch):
    seen = {}

    def fake(host, port, family=0, type=0, proto=0, flags=0):
        seen["family"] = family
        return []

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    net.prefer_ipv4()
    socket.getaddrinfo("api.kite.trade", 443)
    assert seen["family"] == socket.AF_INET


def test_unspec_family_is_forced_to_ipv4(monkeypatch):
    seen = {}

    def fake(host, port, family=0, type=0, proto=0, flags=0):
        seen["family"] = family
        return []

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    net.prefer_ipv4()
    socket.getaddrinfo("api.kite.trade", 443, socket.AF_UNSPEC)
    assert seen["family"] == socket.AF_INET


def test_explicit_ipv6_request_is_respected(monkeypatch):
    seen = {}

    def fake(host, port, family=0, type=0, proto=0, flags=0):
        seen["family"] = family
        return []

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    net.prefer_ipv4()
    socket.getaddrinfo("api.kite.trade", 443, socket.AF_INET6)
    assert seen["family"] == socket.AF_INET6


def test_positional_arguments_are_preserved(monkeypatch):
    seen = {}

    def fake(host, port, family=0, type=0, proto=0, flags=0):
        seen.update(host=host, port=port, type=type, proto=proto, flags=flags)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    net.prefer_ipv4()
    socket.getaddrinfo("api.kite.trade", 443, 0, socket.SOCK_STREAM, 6, 0)
    assert seen == {"host": "api.kite.trade", "port": 443,
                    "type": socket.SOCK_STREAM, "proto": 6, "flags": 0}


def test_reset_restores_the_original(monkeypatch):
    def fake(host, port, family=0, type=0, proto=0, flags=0):
        return ["original"]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    original = socket.getaddrinfo
    net.prefer_ipv4()
    net.reset()
    assert socket.getaddrinfo is original
