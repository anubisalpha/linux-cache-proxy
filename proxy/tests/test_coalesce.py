"""Request coalescing: concurrent misses for one URL share a single fetch."""
import asyncio
import os
import time
from unittest.mock import MagicMock

import pytest
from mitmproxy import http

from cache_proxy import addon, config, store

pytestmark = pytest.mark.skipif(not store.locks_available(), reason="needs fcntl (Linux)")

URL = "https://dl.example.com/tool-setup.exe"


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "COALESCE_WAIT", 5.0)
    store.init_db()


def _flow(url=URL, method="GET", headers=None, fid="f1"):
    flow = MagicMock()
    flow.id = fid
    flow.request.method = method
    flow.request.pretty_url = url
    flow.request.host = url.split("://", 1)[1].split("/", 1)[0].split(":")[0]
    flow.request.path = "/" + url.split("://", 1)[1].split("/", 1)[1]
    flow.request.headers = http.Headers(**(headers or {}))
    return flow


def _lock_is_free(url=URL) -> bool:
    fd = store.try_lock(store.url_hash(url))
    if fd is None:
        return False
    store.release_lock(fd)
    return True


# ---- the lock itself -----------------------------------------------------

def test_lock_is_exclusive_until_released():
    h = store.url_hash(URL)
    fd = store.try_lock(h)
    assert fd is not None
    assert store.try_lock(h) is None
    store.release_lock(fd)
    fd2 = store.try_lock(h)
    assert fd2 is not None
    store.release_lock(fd2)


def test_locks_for_different_urls_are_independent():
    a, b = store.try_lock("a" * 64), store.try_lock("b" * 64)
    assert a is not None and b is not None
    store.release_lock(a)
    store.release_lock(b)


def test_cleanup_removes_only_stale_unheld_locks():
    stale, held = "a" * 64, "b" * 64
    store.release_lock(store.try_lock(stale))
    fd = store.try_lock(held)
    old = time.time() - 7200
    for h in (stale, held):
        os.utime(config.CACHE_DIR / ".locks" / h, (old, old))
    assert store.cleanup_locks(max_age=3600) == 1
    assert not (config.CACHE_DIR / ".locks" / stale).exists()
    assert (config.CACHE_DIR / ".locks" / held).exists()
    store.release_lock(fd)


# ---- which requests coalesce ---------------------------------------------

@pytest.mark.parametrize("kwargs,expected", [
    ({}, True),
    ({"url": "https://cdn.example.com/app.js?v=3"}, True),   # static asset
    ({"url": "https://example.com/index.html"}, False),      # pages never wait
    ({"url": "https://example.com/api/data"}, False),
    ({"method": "POST"}, False),
    ({"headers": {"Range": "bytes=0-9"}}, False),
    ({"headers": {"Authorization": "Bearer x"}}, False),
])
def test_only_download_and_asset_gets_coalesce(kwargs, expected):
    assert addon.CacheAddon._coalescible(_flow(**kwargs)) is expected


def test_disabled_when_wait_is_zero(monkeypatch):
    monkeypatch.setattr(config, "COALESCE_WAIT", 0)
    assert addon.CacheAddon._coalescible(_flow()) is False


def test_never_cache_hosts_do_not_coalesce(monkeypatch):
    monkeypatch.setattr(config, "NEVER_CACHE_HOSTS", ["example.com"])
    assert addon.CacheAddon._coalescible(_flow()) is False


# ---- leader / waiter behaviour -------------------------------------------

def test_first_request_leads_and_second_waits_then_uses_the_cached_copy():
    a = addon.CacheAddon()
    leader, waiter = _flow(fid="L"), _flow(fid="W")
    order = []

    async def scenario():
        await a.requestheaders(leader)
        assert "L" in a._leading

        async def wait():
            await a.requestheaders(waiter)
            order.append("waiter-done")

        task = asyncio.create_task(wait())
        await asyncio.sleep(0.6)
        assert not task.done()  # still waiting on the leader
        store.save_file(URL, "tool-setup.exe", "application/octet-stream", b"x" * 100)
        order.append("stored")
        a._release("L")
        await asyncio.wait_for(task, 3)

    asyncio.run(scenario())
    assert order == ["stored", "waiter-done"]
    assert store.url_hash(URL) in a._known_hashes
    assert a._counters["coalesced_requests"] == 1
    assert "W" not in a._leading  # waiters never hold the lock


def test_many_waiters_all_released_together():
    a = addon.CacheAddon()

    async def scenario():
        await a.requestheaders(_flow(fid="L"))
        tasks = [asyncio.create_task(a.requestheaders(_flow(fid=f"W{i}"))) for i in range(10)]
        await asyncio.sleep(0.5)
        assert not any(t.done() for t in tasks)
        store.save_file(URL, "tool-setup.exe", "t", b"x" * 10)
        a._release("L")
        start = time.monotonic()
        await asyncio.wait_for(asyncio.gather(*tasks), 3)
        return time.monotonic() - start

    assert asyncio.run(scenario()) < 1.0  # released together, not one after another
    assert a._counters["coalesced_requests"] == 10


def test_waiter_gives_up_after_the_cap(monkeypatch):
    monkeypatch.setattr(config, "COALESCE_WAIT", 0.6)
    a = addon.CacheAddon()

    async def scenario():
        await a.requestheaders(_flow(fid="L"))  # leader never finishes
        start = time.monotonic()
        await a.requestheaders(_flow(fid="W"))
        return time.monotonic() - start

    took = asyncio.run(scenario())
    assert 0.5 < took < 2.0
    assert store.url_hash(URL) not in a._known_hashes  # nothing to serve; goes upstream


def test_leader_skips_fetch_if_another_worker_already_stored_it():
    store.save_file(URL, "tool-setup.exe", "t", b"x" * 10)
    a = addon.CacheAddon()  # its index refresh hasn't caught up
    flow = _flow()
    asyncio.run(a.requestheaders(flow))
    assert store.url_hash(URL) in a._known_hashes
    assert flow.id not in a._leading
    assert _lock_is_free()


def test_expired_entry_does_not_count_as_stored():
    store.save_file(URL, "tool-setup.exe", "t", b"x", ttl=-1)
    a = addon.CacheAddon()
    asyncio.run(a.requestheaders(_flow()))
    assert "f1" in a._leading  # still leads a refetch


def test_known_url_skips_coalescing_entirely():
    a = addon.CacheAddon()
    a._known_hashes = {store.url_hash(URL)}
    asyncio.run(a.requestheaders(_flow()))
    assert a._leading == {} and _lock_is_free()


# ---- the lock is released on every way a fetch can end ---------------------

def _leading(a):
    flow = _flow()
    asyncio.run(a.requestheaders(flow))
    assert not _lock_is_free()
    return flow


def _resp(status=200, ctype="application/octet-stream", body=b"x", **headers):
    return http.Response.make(status, body, {"Content-Type": ctype, **headers})


def test_released_when_response_wont_be_stored():
    a = addon.CacheAddon()
    flow = _leading(a)
    flow.response = _resp(404)
    a.responseheaders(flow)
    assert _lock_is_free()


def test_released_when_response_is_too_big_to_store(monkeypatch):
    monkeypatch.setattr(config, "MAX_BUFFER_SIZE", 10)
    a = addon.CacheAddon()
    flow = _leading(a)
    flow.response = _resp(200, body=b"x" * 999)  # Content-Length 999 > limit of 10
    a.responseheaders(flow)
    assert flow.response.stream is True and _lock_is_free()


def test_held_while_buffering_then_released_after_store(monkeypatch):
    monkeypatch.setattr(config, "MIN_CACHE_SIZE", 1)
    a = addon.CacheAddon()
    flow = _leading(a)
    flow.client_conn.peername = ("10.0.0.5", 1)
    flow.response = _resp(200, body=b"x" * 50, **{"Content-Length": "50"})
    a.responseheaders(flow)
    assert not _lock_is_free()  # will be stored: waiters keep waiting
    a.response(flow)
    assert _lock_is_free()
    assert store.get_entry(store.url_hash(URL)) is not None


def test_released_on_flow_error():
    a = addon.CacheAddon()
    flow = _leading(a)
    a.error(flow)
    assert _lock_is_free()


def test_released_even_if_storing_raises(monkeypatch):
    a = addon.CacheAddon()
    flow = _leading(a)
    flow.response = _resp(200)
    monkeypatch.setattr(a, "_store_response", MagicMock(side_effect=RuntimeError("disk full")))
    with pytest.raises(RuntimeError):
        a.response(flow)
    assert _lock_is_free()


def test_lock_dies_with_its_holder():
    """flock is released by the kernel if a worker crashes mid-fetch."""
    import subprocess, sys
    h = store.url_hash(URL)
    code = (
        "import os, sys, time\n"
        "from cache_proxy import config, store\n"
        f"config.CACHE_DIR = __import__('pathlib').Path({str(config.CACHE_DIR)!r})\n"
        f"fd = store.try_lock({h!r}); print('locked' if fd is not None else 'no', flush=True); time.sleep(60)\n"
    )
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                         cwd=str(__import__("pathlib").Path(addon.__file__).parents[1]))
    try:
        assert p.stdout.readline().strip() == "locked"
        assert not _lock_is_free()
        p.kill()
        p.wait()
        assert _lock_is_free()
    finally:
        p.kill()
