"""Supervisor for a pool of proxy worker processes.

mitmproxy runs on one core per process. This starts `min_workers` of them on
the same port (via SO_REUSEPORT, see worker_main.py), adds more when the pool
is busy up to `max_workers`, and retires idle ones back down to the minimum.
It also writes a status file for the web UI and runs the housekeeping jobs
(expiry purge, quota eviction, access-log pruning) so exactly one process
does them.

Usage: python -m cache_proxy.supervisor <mitmdump args...>
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import config, store

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


class Scaler:
    """Decides whether to add or retire a worker. Pure logic, no I/O."""

    UP_SAMPLES = 2  # consecutive busy samples before adding a worker

    def __init__(self, min_workers, max_workers, up_pct, down_pct, idle_seconds):
        self.min_workers = min_workers
        self.max_workers = max_workers
        self.up_pct = up_pct
        self.down_pct = down_pct
        self.idle_seconds = idle_seconds
        self._high = 0
        self._idle_since: Optional[float] = None

    def decide(self, cpus: list, now: float) -> str:
        """cpus: each worker's CPU as a percent of one core. Returns
        "up", "down" or "hold"."""
        n = len(cpus)
        if n < self.min_workers:
            return "up"
        avg = sum(cpus) / n if n else 0.0

        self._high = self._high + 1 if avg >= self.up_pct else 0
        if self._high >= self.UP_SAMPLES and n < self.max_workers:
            self._high = 0
            self._idle_since = None
            return "up"

        # Only shrink if the remaining workers would still be comfortably
        # under the scale-up threshold, otherwise it would flap.
        can_shrink = n > self.min_workers and avg < self.down_pct and avg * n / (n - 1) < self.up_pct
        if can_shrink:
            if self._idle_since is None:
                self._idle_since = now
            elif now - self._idle_since >= self.idle_seconds:
                self._idle_since = None
                return "down"
        else:
            self._idle_since = None
        return "hold"


def read_cpu_ticks(pid: int) -> Optional[int]:
    """utime+stime of a process, in clock ticks, from /proc."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # comm (field 2) may contain spaces/parens; everything after the last
    # ")" is well-formed. utime/stime are fields 14/15 (1-based).
    fields = data.rsplit(")", 1)[1].split()
    return int(fields[11]) + int(fields[12])


def _socket_inodes(pid: int) -> set:
    inodes = set()
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
    except OSError:
        pass
    return inodes


def _count_sockets(pid: int, port: int, state: str) -> int:
    """Sockets owned by pid, on local `port`, in TCP `state` (hex, e.g. "01"
    established, "0A" listening), from /proc/net/tcp{,6}."""
    inodes = _socket_inodes(pid)
    if not inodes:
        return 0
    count = 0
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            if len(f) > 9 and f[3] == state and int(f[1].rsplit(":", 1)[1], 16) == port and f[9] in inodes:
                count += 1
    return count


def client_connections(pid: int, port: int) -> int:
    """Established client connections a process holds on its listen port."""
    return _count_sockets(pid, port, "01")


def is_listening(pid: int, port: int) -> bool:
    return _count_sockets(pid, port, "0A") > 0


class Worker:
    def __init__(self, args: list):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "cache_proxy.worker_main", *args],
            stdout=None,
            stderr=None,
        )
        self.started = time.time()
        self.retiring = False
        self.ready = False  # becomes True once it is listening on the port
        self._last_ticks = read_cpu_ticks(self.proc.pid)
        self._last_time = time.monotonic()
        self.cpu = 0.0

    @property
    def pid(self) -> int:
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc.poll() is None

    def state(self) -> str:
        if self.retiring:
            return "retiring"
        return "active" if self.ready else "starting"

    def sample(self) -> None:
        ticks = read_cpu_ticks(self.pid)
        now = time.monotonic()
        if ticks is not None and self._last_ticks is not None and now > self._last_time:
            self.cpu = 100.0 * (ticks - self._last_ticks) / CLK_TCK / (now - self._last_time)
        self._last_ticks, self._last_time = ticks, now


class Supervisor:
    def __init__(self, worker_args: list, port: int, confdir: Path):
        self.worker_args = worker_args
        self.port = port
        self.confdir = confdir
        self.workers: list = []
        self.stopping = False
        self.scaler = Scaler(
            config.MIN_WORKERS,
            config.MAX_WORKERS,
            config.SCALE_UP_CPU_PCT,
            config.SCALE_DOWN_CPU_PCT,
            config.SCALE_DOWN_IDLE_SECONDS,
        )
        self._last_hourly = 0.0
        self._last_daily = 0.0

    def _spawn(self) -> None:
        w = Worker(self.worker_args)
        self.workers.append(w)
        print(f"supervisor: started worker pid {w.pid} ({len(self.workers)} running)", flush=True)

    def _start_initial(self) -> None:
        # The first worker generates the mitmproxy CA on startup. Bring it up
        # alone and wait for the cert so the others don't all race to create
        # (and overwrite) it.
        self._spawn()
        cert = self.confdir / "mitmproxy-ca-cert.pem"
        deadline = time.time() + 60
        while not cert.exists() and time.time() < deadline and self.workers[0].alive():
            time.sleep(0.5)
        while len(self.workers) < config.MIN_WORKERS:
            self._spawn()

    def _reap(self) -> None:
        for w in list(self.workers):
            if w.alive():
                continue
            self.workers.remove(w)
            if w.retiring:
                print(f"supervisor: worker pid {w.pid} retired", flush=True)
            else:
                print(f"supervisor: worker pid {w.pid} exited unexpectedly ({w.proc.returncode})", flush=True)

    def _retire_one(self) -> None:
        candidates = [w for w in self.workers if not w.retiring]
        if len(candidates) <= config.MIN_WORKERS:
            return
        # SIGTERM drops a worker's open connections, and a long download
        # uses almost no CPU, so idle CPU alone doesn't mean it's safe.
        # Only retire a worker that has no client connections; otherwise
        # try again next sample.
        for w in sorted(candidates, key=lambda w: w.cpu):
            if client_connections(w.pid, self.port) == 0:
                w.retiring = True
                w.proc.terminate()
                return

    def _write_status(self) -> None:
        active = [w for w in self.workers if w.state() == "active"]
        status = {
            "ts": time.time(),
            "min_workers": config.MIN_WORKERS,
            "max_workers": config.MAX_WORKERS,
            "avg_cpu": (sum(w.cpu for w in active) / len(active)) if active else 0.0,
            "workers": [
                {
                    "pid": w.pid,
                    "state": w.state(),
                    "cpu": round(w.cpu, 1),
                    "connections": client_connections(w.pid, self.port),
                    "uptime": int(time.time() - w.started),
                }
                for w in self.workers
            ],
        }
        path = config.WORKER_STATUS_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(status))
            os.replace(tmp, path)
        except OSError as e:
            print(f"supervisor: could not write status file: {e}", flush=True)

    def _housekeeping(self, now: float) -> None:
        if now - self._last_hourly >= 3600:
            self._last_hourly = now
            try:
                purged = store.purge_expired()
                evicted = store.evict_to_quota()
                store.cleanup_locks()
                if purged or evicted:
                    print(f"supervisor: purged {purged} expired, evicted {evicted} over quota", flush=True)
            except Exception as e:  # housekeeping must never take the pool down
                print(f"supervisor: housekeeping failed: {e}", flush=True)
        if now - self._last_daily >= 86400:
            self._last_daily = now
            try:
                pruned = store.prune_access_log()
                if pruned:
                    print(f"supervisor: pruned {pruned} old access-log rows", flush=True)
            except Exception as e:
                print(f"supervisor: access-log prune failed: {e}", flush=True)

    def _shutdown(self, *_):
        self.stopping = True

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)
        store.init_db()
        self._start_initial()

        while not self.stopping:
            time.sleep(config.SAMPLE_INTERVAL)
            if self.stopping:
                # systemd signals the whole service at once, so workers may
                # already be gone; that's a shutdown, not a crash.
                break
            self._reap()
            # Replace crashed workers up to the minimum straight away.
            while not self.stopping and len([w for w in self.workers if not w.retiring]) < config.MIN_WORKERS:
                self._spawn()
            for w in self.workers:
                w.sample()
                if not w.ready and is_listening(w.pid, self.port):
                    w.ready = True
            active = [w for w in self.workers if not w.retiring]
            # A booting worker burns CPU importing mitmproxy and isn't taking
            # traffic yet; don't let it skew the load figure, and don't make
            # scaling decisions until the pool has settled.
            if any(not w.ready for w in active):
                action = "hold"
            else:
                action = self.scaler.decide([w.cpu for w in active], time.monotonic())
            if action == "up":
                self._spawn()
            elif action == "down":
                self._retire_one()
            self._write_status()
            self._housekeeping(time.time())

        for w in self.workers:
            if w.alive():
                w.proc.terminate()
        deadline = time.time() + 10
        for w in self.workers:
            try:
                w.proc.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                w.proc.kill()
        return 0


def main() -> int:
    port = int(os.environ.get("CACHE_PROXY_PORT", "8080"))
    confdir = Path(os.environ.get("CACHE_PROXY_CONFDIR", "/var/lib/cache-proxy/mitmproxy-ca"))
    return Supervisor(sys.argv[1:], port, confdir).run()


if __name__ == "__main__":
    sys.exit(main())
