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


def env_for(state: Path, slots: int, tools: dict[str, dict[str, object]], base_path: str | None = None) -> dict[str, str]:
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


def install_shims(state: Path, tools: dict[str, dict[str, object]]) -> Path:
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


def is_heavy(rule: dict[str, object] | None, args: list[str]) -> bool:
    """Heavy when a configured flag appears, or when the first positional word is a configured subcommand.

    The values of `value_options` (for example `-C <dir>`) are skipped, so they are never taken for the subcommand.
    """
    if not rule:
        return False
    flags = set(rule.get("flags", []))  # type: ignore[arg-type]
    if flags and any(a in flags for a in args):
        return True
    subcommands = set(rule.get("subcommands", []))  # type: ignore[arg-type]
    if not subcommands:
        return False
    value_options = set(rule.get("value_options", []))  # type: ignore[arg-type]
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

SNAPSHOT_IDENTITY = {"GIT_AUTHOR_NAME": "workboard-compile", "GIT_AUTHOR_EMAIL": "compile@localhost",
                     "GIT_COMMITTER_NAME": "workboard-compile", "GIT_COMMITTER_EMAIL": "compile@localhost"}


def _caller_scope() -> tuple[str, str, str, str]:
    return (os.environ.get("WORKBOARD_WORKER", ""), os.environ.get("WORKBOARD_HARNESS", ""),
            os.environ.get("WORKBOARD_ROLE", "") or "interactive", os.environ.get("WORKBOARD_TASK", ""))


def _check_directives(folder: Path, on_abort=None) -> int:
    db = folder.parent / "workboard.db"
    if not db.is_file():
        return 0
    import fnmatch
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='directives'").fetchone():
                return 0
            rows = conn.execute(
                "SELECT * FROM directives WHERE active=1 AND (expires_at IS NULL OR expires_at>?) ORDER BY seq",
                (time.time(),),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return 0
    _, harness, role, task_id = _caller_scope()
    seen = set(filter(None, os.environ.get("WORKBOARD_SEEN_DIRECTIVES", "").split(",")))
    added: list[str] = []
    for row in rows:
        if row["scope_harness"] and row["scope_harness"] != harness:
            continue
        if row["scope_role"] and row["scope_role"] != role:
            continue
        if row["scope_task"] and (not task_id or not fnmatch.fnmatchcase(task_id, str(row["scope_task"]))):
            continue
        did = str(row["directive_id"])
        if row["mode"] in ("wrap_up", "cancel_builds", "abort_leases"):
            if on_abort is not None:
                on_abort()
            print(f"workboard directive [{did} {row['mode']}]: {row['message']}\n"
                  "Do not start or retry builds while this directive is active; save your notes and wrap up.",
                  file=sys.stderr, flush=True)
            return 1
        if row["mode"] == "instruction" and did not in seen:
            print(f"[workboard notice {did}]: {row['message']}", file=sys.stderr, flush=True)
            seen.add(did)
            added.append(did)
    if added:
        os.environ["WORKBOARD_SEEN_DIRECTIVES"] = ",".join(sorted(seen))
    return 0


def _register_build(folder: Path, *, pid: int, pgid: int, slot: str, cmd: str) -> str:
    db = folder.parent / "workboard.db"
    if not db.is_file():
        return ""
    import sqlite3
    import uuid
    worker_id, harness, role, task_id = _caller_scope()
    bid = f"compile-{pid}-{uuid.uuid4().hex[:8]}"
    try:
        conn = sqlite3.connect(str(db), timeout=2.0)
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='active_builds'").fetchone():
                return ""
            conn.execute(
                "INSERT OR REPLACE INTO active_builds (build_id, pid, pgid, slot, worker, harness, role, task_id, "
                "cwd, cmd, started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (bid, pid, pgid, slot, worker_id, harness, role, task_id, os.getcwd(), cmd[:200], time.time()),
            )
            conn.commit()
            return bid
        finally:
            conn.close()
    except Exception:
        return ""


def _unregister_build(folder: Path, build_id: str) -> None:
    if not build_id:
        return
    db = folder.parent / "workboard.db"
    if not db.is_file():
        return
    import sqlite3
    try:
        conn = sqlite3.connect(str(db), timeout=2.0)
        try:
            conn.execute("DELETE FROM active_builds WHERE build_id=?", (build_id,))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    import subprocess
    res = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {res.stderr.strip()}")
    return res.stdout.strip()


def _checkout_root(start: Path) -> Path | None:
    import subprocess
    probe = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start, capture_output=True, text=True, check=False)
    return Path(probe.stdout.strip()).resolve() if probe.returncode == 0 and probe.stdout.strip() else None


def _preferred_slot(folder: Path, checkout: str) -> int:
    try:
        return int(json.loads((folder / "affinity.json").read_text(encoding="utf-8")).get(checkout, 0))
    except Exception:
        return 0


def _remember_slot(folder: Path, checkout: str, slot: int) -> None:
    try:
        path = folder / "affinity.json"
        table = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        table[checkout] = slot
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(table), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _enter_warm_slot(folder: Path, slot: int, checkout: Path, args: list[str]) -> list[str]:
    """Snapshot the caller's working tree through a scratch index and check it out in slot N's fixed checkout."""
    import tempfile
    cwd = Path.cwd().resolve()
    common = Path(_git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    place = folder / "worktrees" / f"slot-{slot}"
    index = Path(_git(checkout, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    with tempfile.TemporaryDirectory(prefix="wb-warm-index-") as tmp:
        scratch = Path(tmp) / "index"
        if index.exists():
            shutil.copyfile(index, scratch)
        git_env = {**os.environ, "GIT_INDEX_FILE": str(scratch)}
        _git(checkout, "add", "-A", env=git_env)
        tree = _git(checkout, "write-tree", env=git_env)
    head = _git(checkout, "rev-parse", "HEAD")
    commit = _git(checkout, "commit-tree", tree, "-p", head, "-m", "workboard warm compile snapshot",
                  env={**os.environ, **SNAPSHOT_IDENTITY})
    if not (place / ".git").exists():
        place.parent.mkdir(parents=True, exist_ok=True)
        _git(common.parent, "worktree", "add", "--detach", "--force", str(place), commit)
    else:
        _git(place, "checkout", "--force", "--detach", commit)
        _git(place, "clean", "-fdq")
    rel = cwd.relative_to(checkout)
    os.chdir(place / rel)
    os.environ["PWD"] = str(place / rel)
    os.environ["WORKBOARD_WARM_DIR"] = str(place)

    def remap(arg: str) -> str:
        prefix, sep, value = arg.partition("=") if arg.startswith("--") and "=" in arg else ("", "", arg)
        if not value.startswith("/"):
            return arg
        real = Path(os.path.realpath(value))
        return prefix + sep + str(place / real.relative_to(checkout)) if real.is_relative_to(checkout) else arg

    return [remap(a) for a in args]


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


def _acquire(folder: Path, slots: int, ident: dict[str, object], prefer: int = 0):
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
        while True:
            if _check_directives(folder, on_abort=(lambda: record(
                folder, "abandoned", reason="directive", wait_s=round(time.time() - (queued_at or time.time()), 1), **ident
            )) if queued_at is not None else None):
                line.close()
                sys.exit(1)
            try:
                fcntl.flock(line.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                queued()
                time.sleep(POLL_S)
        handles = [open(folder / f"slot-{i}.lock", "a+") for i in range(1, slots + 1)]
        try:
            while True:
                if _check_directives(folder, on_abort=(lambda: record(
                    folder, "abandoned", reason="directive", wait_s=round(time.time() - (queued_at or time.time()), 1), **ident
                )) if queued_at is not None else None):
                    for other in handles:
                        other.close()
                    sys.exit(1)
                for i, handle in sorted(enumerate(handles, 1), key=lambda pair: (pair[0] != prefer, pair[0])):
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
        except BaseException:
            for other in handles:
                other.close()
            raise
    finally:
        if not line.closed:
            line.close()  # the next waiter in line may now look for a slot


def _watch_release(build_pid: int, slot: int, acquired_at: float, lock_fd: int, folder: Path,
                   ident: dict[str, object], build_id: str = "") -> None:
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
        _unregister_build(folder, build_id)
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
    rule = tools.get(tool)
    if os.environ.get(HELD_ENV) or slots <= 0 or not str(folder) or not is_heavy(rule, args):
        os.execv(exe, [exe, *args])
    if _check_directives(folder):
        sys.exit(1)
    warm_checkout = _checkout_root(Path.cwd()) if bool((rule or {}).get("warm")) and os.environ.get("WORKBOARD_WARM") != "0" else None
    prefer = _preferred_slot(folder, str(warm_checkout)) if warm_checkout else 0
    ident: dict[str, object] = {"pid": os.getpid(), "cwd": os.getcwd(), "cmd": " ".join([tool, *args])[:200]}
    if warm_checkout:
        ident["warm"] = True
    slot, handle, queued_at = _acquire(folder, slots, ident, prefer=prefer)
    for sig in STOP_SIGNALS:
        signal.signal(sig, signal.SIG_DFL)  # from here on a stop belongs to the build
    acquired_at = time.time()
    record(folder, "acquired", slot=slot, slots=slots,
           wait_s=round(acquired_at - queued_at, 1) if queued_at else 0.0, **ident)
    if warm_checkout:
        try:
            args = _enter_warm_slot(folder, slot, warm_checkout, args)
            _remember_slot(folder, str(warm_checkout), slot)
        except Exception as exc:
            record(folder, "warm_failed", slot=slot, error=str(exc)[:200], **ident)
            sys.exit(f"workboard compile queue: could not prepare warm checkout slot {slot}: {exc}\n"
                     "Set WORKBOARD_WARM=0 to build in the caller's checkout instead.")
    own_pgid = os.getpid() if os.getpgrp() == os.getpid() else 0
    bid = _register_build(folder, pid=os.getpid(), pgid=own_pgid, slot=str(slot), cmd=str(ident["cmd"]))
    _watch_release(os.getpid(), slot, acquired_at, handle.fileno(), folder, ident, bid)
    os.environ[HELD_ENV] = str(slot)  # nested heavy commands run inside this slot
    os.set_inheritable(handle.fileno(), True)  # the lock lives exactly as long as the build process
    os.execv(exe, [exe, *args])


if __name__ == "__main__":
    shim_main(sys.argv[1:])
