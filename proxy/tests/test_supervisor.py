"""Scaling decisions and the /proc helpers the supervisor relies on."""
import os
import socket

import pytest

from cache_proxy import supervisor
from cache_proxy.supervisor import Scaler


def _scaler(**kw):
    args = dict(min_workers=2, max_workers=4, up_pct=70, down_pct=20, idle_seconds=300)
    args.update(kw)
    return Scaler(**args)


def test_below_minimum_asks_for_a_worker():
    assert _scaler().decide([10.0], now=0) == "up"


def test_scales_up_only_after_sustained_load():
    s = _scaler()
    assert s.decide([90, 90], now=0) == "hold"  # one busy sample isn't enough
    assert s.decide([90, 90], now=5) == "up"


def test_a_single_spike_does_not_scale_up():
    s = _scaler()
    assert s.decide([90, 90], now=0) == "hold"
    assert s.decide([10, 10], now=5) == "hold"
    assert s.decide([90, 90], now=10) == "hold"  # counter was reset by the quiet sample


def test_never_exceeds_max_workers():
    s = _scaler(max_workers=2)
    for t in range(0, 50, 5):
        assert s.decide([99, 99], now=t) == "hold"


def test_scales_down_after_idle_period_but_not_below_min():
    s = _scaler()
    assert s.decide([2, 2, 2], now=0) == "hold"
    assert s.decide([2, 2, 2], now=299) == "hold"
    assert s.decide([2, 2, 2], now=301) == "down"
    # Back at the minimum: idle forever, never shrinks further.
    assert s.decide([2, 2], now=1000) == "hold"
    assert s.decide([2, 2], now=5000) == "hold"


def test_activity_resets_the_idle_timer():
    s = _scaler()
    s.decide([2, 2, 2], now=0)
    s.decide([40, 40, 40], now=200)  # not idle any more
    assert s.decide([2, 2, 2], now=310) == "hold"  # timer restarted at 310
    assert s.decide([2, 2, 2], now=620) == "down"


def test_does_not_shrink_if_survivors_would_be_overloaded():
    # avg 19% over 3 workers -> 28% over 2: fine. But with a low up threshold
    # the remaining workers would immediately trigger a scale-up (flapping).
    s = _scaler(up_pct=25)
    s.decide([19, 19, 19], now=0)
    assert s.decide([19, 19, 19], now=400) == "hold"


@pytest.mark.skipif(not os.path.exists("/proc/self/stat"), reason="needs /proc")
def test_read_cpu_ticks_for_this_process_grows():
    a = supervisor.read_cpu_ticks(os.getpid())
    sum(i * i for i in range(2_000_000))
    b = supervisor.read_cpu_ticks(os.getpid())
    assert a is not None and b is not None and b >= a


def test_read_cpu_ticks_missing_pid_is_none():
    assert supervisor.read_cpu_ticks(2 ** 22 + 12345) is None


@pytest.mark.skipif(not os.path.exists("/proc/net/tcp"), reason="needs /proc")
def test_client_connections_counts_established_on_listen_port():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    assert supervisor.client_connections(os.getpid(), port) == 0
    c = socket.create_connection(("127.0.0.1", port))
    accepted, _ = srv.accept()
    try:
        # The accepted socket is local-port == listen port and belongs to us.
        assert supervisor.client_connections(os.getpid(), port) == 1
    finally:
        accepted.close()
        c.close()
        srv.close()


@pytest.mark.skipif(not os.path.exists("/proc/net/tcp"), reason="needs /proc")
def test_is_listening_only_true_for_a_bound_listener():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    assert not supervisor.is_listening(os.getpid(), port)  # bound, not yet listening
    srv.listen()
    assert supervisor.is_listening(os.getpid(), port)
    srv.close()
    assert not supervisor.is_listening(os.getpid(), port)
