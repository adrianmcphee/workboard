"""The compile queue: heavy build commands share a fixed number of slots instead of oversubscribing the machine.

When `[compile]` is configured, every agent, guard, landing and build runs with a shim directory first on
its PATH. Each shim stands in for one configured tool (for example `cargo` or `cmake`). A heavy invocation
of that tool holds an exclusive `flock` on one slot file for its whole life; everything else, and anything
started inside a heavy command, passes straight through to the real tool.

Every heavy command appends `queued` (only when it had to wait), `acquired`, `released` and, for a waiter
stopped by a signal, `abandoned` records to `compile/events.jsonl` in the state directory, each with a
memory snapshot, so the slot count can be judged against measured waits and memory.

This file also runs on its own as the shim (`python3 compile_queue.py <tool> <args...>`), so it imports
nothing from the package.
"""
from __future__ import annotations

import ctypes
import fcntl
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import sys
import time

STOP_SIGNALS = (signal.SIGTERM, signal.SIGQUIT, signal.SIGHUP, signal.SIGINT)
HELD_ENV = "WORKBOARD_COMPILE_HELD"
POLL_S = 0.2
RECENT = 2000  # event lines read for the queue view


# ------------------------------------------------------------------ configuration shared with the shim

def queue_dir(state: Path) -> Path:
    return state / "compile"


def env_for(state: Path, slots: int, tools: dict[str, dict[str, list[str]]], base_path: str | None = None) -> dict[str, str]:
    """The environment that routes a process's heavy commands through the queue. Empty when the queue is off."""
    if slots <= 0 or not tools:
        return {}
    shims = install_shims(state, tools)
    return {"PATH": str(shims) + os.pathsep + (base_path if base_path is not None else os.environ.get("PATH", "")),
            "WORKBOARD_COMPILE_SHIMS": str(shims), "WORKBOARD_COMPILE_DIR": str(queue_dir(state)),
            "WORKBOARD_COMPILE_SLOTS": str(slots), "WORKBOARD_COMPILE_TOOLS": json.dumps(tools, sort_keys=True)}


def env(cfg, base_path: str | None = None) -> dict[str, str]:
    """`env_for` from a loaded Config (duck-typed, so this file stays importable on its own)."""
    return env_for(cfg.state, cfg.compile_slots, cfg.compile_tools, base_path)


def install_shims(state: Path, tools: dict[str, dict[str, list[str]]]) -> Path:
    """Write one executable shim per configured tool. Rewritten each time, so a moved interpreter is picked up."""
    shims = queue_dir(state) / "shims"
    shims.mkdir(parents=True, exist_ok=True)
    here = Path(__file__).resolve()
    for tool in tools:
        target = shims / tool
        # -I keeps this file's own folder (which holds queue.py, config.py, ...) off sys.path.
        text = f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -I {shlex.quote(str(here))} {shlex.quote(tool)} \"$@\"\n"
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            tmp = target.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.chmod(0o755)
            tmp.replace(target)
    return shims


def is_heavy(rule: dict[str, list[str]] | None, args: list[str]) -> bool:
    """Heavy when a configured flag appears, or when the first positional word is a configured subcommand.

    The values of `value_options` (for example `-C <dir>`) are skipped, so they are never taken for the subcommand.
    """
    if not rule:
        return False
    flags = set(rule.get("flags", []))
    if flags and any(a in flags for a in args):
        return True
    subcommands = set(rule.get("subcommands", []))
    if not subcommands:
        return False
    value_options = set(rule.get("value_options", []))
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg in value_options:
            skip = True
        elif not arg.startswith(("-", "+")):
            return arg in subcommands
    return False


# ------------------------------------------------------------------ memory, read in-process

def memory() -> dict[str, float]:
    """Total memory, the share available, and (macOS) the compressor's size. Empty when the OS gives no reading."""
    try:
        if sys.platform == "darwin":
            libc = ctypes.CDLL(None, use_errno=True)

            def sysctl(name: str) -> int:
                buf, size = ctypes.c_uint64(0), ctypes.c_size_t(8)
                if libc.sysctlbyname(name.encode(), ctypes.byref(buf), ctypes.byref(size), None, ctypes.c_size_t(0)):
                    raise OSError(ctypes.get_errno(), name)
                return buf.value & ((1 << (8 * size.value)) - 1)

            return {"mem_total_gib": round(sysctl("hw.memsize") / 2**30, 1),
                    "mem_available_pct": float(sysctl("kern.memorystatus_level")),
                    "compressor_gib": round(sysctl("vm.compressor_bytes_used") / 2**30, 2)}
        if sys.platform.startswith("linux"):
            info: dict[str, int] = {}
            with open("/proc/meminfo", encoding="ascii") as fh:
                for line in fh:
                    key, _, rest = line.partition(":")
                    info[key] = int(rest.split()[0]) * 1024
            total, avail = info["MemTotal"], info["MemAvailable"]
            return {"mem_total_gib": round(total / 2**30, 1), "mem_available_pct": round(100.0 * avail / total, 1)}
    except Exception:
        pass
    return {}


def record(folder: Path, event: str, **fields: object) -> None:
    """Append one JSON line. A logging failure never affects the build."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ts": round(time.time(), 3), "event": event, **fields, **memory()}) + "\n"
        fd = os.open(folder / "events.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except Exception:
        pass


# ------------------------------------------------------------------ the queue view for status, dashboard, interventions

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def view(state: Path, slots: int, now: float | None = None, alive=_alive) -> dict[str, object]:
    """Who holds a slot, who waits and for how long, from the event log and live process IDs."""
    now = now or time.time()
    latest: dict[int, dict[str, object]] = {}
    waits: list[float] = []
    path = queue_dir(state) / "events.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-RECENT:]
    except OSError:
        lines = []
    for line in lines:
        try:
            ev = json.loads(line)
            pid = int(ev["pid"])
        except (ValueError, KeyError, TypeError):
            continue
        if ev.get("event") == "acquired":
            waits.append(float(ev.get("wait_s") or 0.0))
            latest[pid] = ev
        elif ev.get("event") == "queued":
            latest[pid] = ev
        elif ev.get("event") in ("released", "abandoned") and latest.get(pid, {}).get("event") in ("acquired", "queued"):
            latest.pop(pid, None)
    holders = [{"pid": p, "cmd": e.get("cmd"), "cwd": e.get("cwd"), "held_s": round(now - float(e["ts"]))}
               for p, e in latest.items() if e["event"] == "acquired" and alive(p)]
    waiters = [{"pid": p, "cmd": e.get("cmd"), "cwd": e.get("cwd"), "wait_s": round(now - float(e["ts"]))}
               for p, e in latest.items() if e["event"] == "queued" and alive(p)]
    recent = sorted(waits[-50:])
    return {"enabled": slots > 0, "slots": slots, "holders": holders, "waiters": waiters,
            "longest_wait_s": max((w["wait_s"] for w in waiters), default=0),
            "recent_p90_wait_s": round(recent[int(0.9 * (len(recent) - 1))], 1) if recent else 0.0,
            "memory": memory()}


# ------------------------------------------------------------------ the shim

def _real_tool(name: str, shims: str) -> str:
    keep = [p for p in os.environ.get("PATH", "").split(os.pathsep)
            if p and (not shims or os.path.realpath(p) != os.path.realpath(shims))]
    found = shutil.which(name, path=os.pathsep.join(keep))
    if not found:
        sys.exit(f"workboard compile queue: {name} not found on PATH")
    return found


def _on_stop(callback) -> None:
    """Exit cleanly on a stop signal, so no crash report is raised. `exec` restores the defaults."""
    def handler(signum, _frame):
        callback(signum)
        os._exit(128 + signum)
    for sig in STOP_SIGNALS:
        signal.signal(sig, handler)


def _acquire(folder: Path, slots: int, ident: dict[str, object]):
    """Take one slot. Waiters line up on a queue lock first, so a newcomer never jumps a long wait."""
    folder.mkdir(parents=True, exist_ok=True)
    queued_at: float | None = None

    def queued() -> None:
        nonlocal queued_at
        if queued_at is None:
            queued_at = time.time()
            _on_stop(lambda signum: record(folder, "abandoned", signal=signum,
                                           wait_s=round(time.time() - (queued_at or time.time()), 1), **ident))
            record(folder, "queued", slots=slots, **ident)
            print(f"workboard: all {slots} compile slot(s) busy; queued", file=sys.stderr, flush=True)

    line = open(folder / "queue.lock", "a+")
    try:
        fcntl.flock(line.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        queued()
        fcntl.flock(line.fileno(), fcntl.LOCK_EX)
    handles = [open(folder / f"slot-{i}.lock", "a+") for i in range(1, slots + 1)]
    try:
        while True:
            for i, handle in enumerate(handles, 1):
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                for other in handles:
                    if other is not handle:
                        other.close()
                return i, handle, queued_at
            queued()
            time.sleep(POLL_S)
    finally:
        line.close()  # the next waiter in line may now look for a slot


def _watch_release(build_pid: int, slot: int, acquired_at: float, lock_fd: int, folder: Path,
                   ident: dict[str, object]) -> None:
    """Fork a detached watcher that writes `released` when the build exits. It holds no lock and no pipe."""
    try:
        pid = os.fork()
    except OSError:
        return
    if pid:
        try:
            os.waitpid(pid, 0)  # the intermediate child exits at once
        except ChildProcessError:
            pass
        return
    try:
        _on_stop(lambda _signum: None)
        os.setsid()
        if os.fork():
            os._exit(0)
        os.close(lock_fd)
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        os.closerange(3, 65536)
        if hasattr(select, "kqueue"):
            kq = select.kqueue()
            try:
                kq.control([select.kevent(build_pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                          select.KQ_NOTE_EXIT)], 1, None)
            except OSError:
                pass  # the build already exited
        else:
            while _alive(build_pid):
                time.sleep(1.0)
        record(folder, "released", slot=slot, hold_s=round(time.time() - acquired_at, 1), **ident)
    finally:
        os._exit(0)


def shim_main(argv: list[str]) -> None:
    tool, args = argv[0], argv[1:]
    exe = _real_tool(tool, os.environ.get("WORKBOARD_COMPILE_SHIMS", ""))
    try:
        tools = json.loads(os.environ.get("WORKBOARD_COMPILE_TOOLS", "{}"))
        slots = int(os.environ.get("WORKBOARD_COMPILE_SLOTS", "0"))
    except ValueError:
        tools, slots = {}, 0
    folder = Path(os.environ.get("WORKBOARD_COMPILE_DIR", ""))
    if os.environ.get(HELD_ENV) or slots <= 0 or not str(folder) or not is_heavy(tools.get(tool), args):
        os.execv(exe, [exe, *args])
    ident = {"pid": os.getpid(), "cwd": os.getcwd(), "cmd": " ".join([tool, *args])[:200]}
    slot, handle, queued_at = _acquire(folder, slots, ident)
    for sig in STOP_SIGNALS:
        signal.signal(sig, signal.SIG_DFL)  # from here on a stop belongs to the build
    acquired_at = time.time()
    record(folder, "acquired", slot=slot, slots=slots,
           wait_s=round(acquired_at - queued_at, 1) if queued_at else 0.0, **ident)
    _watch_release(os.getpid(), slot, acquired_at, handle.fileno(), folder, ident)
    os.environ[HELD_ENV] = str(slot)  # nested heavy commands run inside this slot
    os.set_inheritable(handle.fileno(), True)  # the lock lives exactly as long as the build process
    os.execv(exe, [exe, *args])


if __name__ == "__main__":
    shim_main(sys.argv[1:])
