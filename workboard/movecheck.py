"""What would be lost moving a project to another machine (`move-check`), and archiving it (`move-archive`)."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile

from . import gitops, queue, store
from .config import Config

ARCHIVE_SKIP_PREFIXES = ("refs/remotes/", "refs/tags/", "refs/archive/", "refs/stash")

STATE_PARTS = (("workboard.db", "queue, attempts, events and audit records"),
               ("handoffs", "prompts, takeover notes, review verdicts"),
               ("builds", "build records and sign-off verdicts"),
               ("audits", "audit evidence, findings and reports"),
               ("logs", "agent, landing and build output"),
               ("compile", "compile queue event log"))


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for folder, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(folder, name))
            except OSError:
                pass
    return total


def _unique(cfg: Config, cwd: Path, ref: str) -> tuple[int, int]:
    """Commits on `ref` that no remote ref holds, and how many of those have no equivalent patch on main."""
    ahead = int(gitops.out(cwd, "rev-list", "--count", ref, "--not", "--remotes") or 0)
    if not ahead:
        return 0, 0
    cherry = gitops.git(cwd, "cherry", cfg.remote_main, ref).stdout.splitlines()
    return ahead, min(ahead, sum(1 for line in cherry if line.startswith("+")))


def check(cfg: Config, conn: sqlite3.Connection) -> dict[str, object]:
    repo = cfg.repo
    refs = []
    for ref in gitops.out(repo, "for-each-ref", "--format=%(refname)").splitlines():
        if ref.startswith("refs/remotes/") or ref == "refs/stash":
            continue
        ahead, unique = _unique(cfg, repo, ref)
        if ahead:
            refs.append({"ref": ref, "ahead": ahead, "unique": unique})
    stashes = [line for line in gitops.git(repo, "stash", "list", "--format=%gd %h %s").stdout.splitlines() if line]
    worktrees = []
    for block in gitops.out(repo, "worktree", "list", "--porcelain").split("\n\n"):
        fields = dict(line.split(" ", 1) if " " in line else (line, "") for line in block.splitlines())
        path = fields.get("worktree")
        if not path:
            continue
        if not Path(path).is_dir():
            worktrees.append({"path": path, "missing": True, "dirty": 0, "detached_unique": 0})
            continue
        dirty = len([line for line in gitops.dirty(Path(path)).splitlines() if line])
        detached = 0 if "branch" in fields else _unique(cfg, Path(path), "HEAD")[1]
        worktrees.append({"path": path, "branch": fields.get("branch", "(detached)"), "missing": False,
                          "dirty": dirty, "detached_unique": detached})
    marks = ",".join("?" * len(store.IN_FLIGHT))
    flying = {r[0]: r[1] for r in conn.execute(
        f"SELECT status, COUNT(*) FROM tasks WHERE status IN ({marks}) GROUP BY status", store.IN_FLIGHT)}
    waiting = {r[0]: r[1] for r in conn.execute(
        "SELECT status, COUNT(*) FROM tasks WHERE status IN ('READY','BLOCKED','NEEDS_TRIAGE') GROUP BY status")}
    state_paths = [{"path": str(cfg.state / name), "holds": what, "bytes": _size(cfg.state / name)}
                   for name, what in STATE_PARTS if (cfg.state / name).exists()]
    would_lose = (any(r["unique"] for r in refs) or bool(stashes) or bool(flying)
                  or any(w["dirty"] or w["detached_unique"] for w in worktrees))
    return {"would_lose": would_lose, "draining": queue.draining(conn), "refs": refs, "stashes": stashes,
            "worktrees": worktrees, "tasks_in_flight": flying, "tasks_waiting": waiting, "state_paths": state_paths}


def lines(report: dict[str, object]) -> list[str]:
    out = []
    unique = [r for r in report["refs"] if r["unique"]]  # type: ignore[union-attr]
    landed = [r for r in report["refs"] if not r["unique"]]  # type: ignore[union-attr]
    out.append(f"refs with commits on no remote: {len(unique)} unique, {len(landed)} already on main by patch")
    out += [f"  LOSE {r['ref']}: {r['unique']} of {r['ahead']} commit(s) have no equivalent on main" for r in unique]
    out.append(f"stashes: {len(report['stashes'])}")  # type: ignore[arg-type]
    out += [f"  LOSE {s}" for s in report["stashes"]]  # type: ignore[union-attr]
    for w in report["worktrees"]:  # type: ignore[union-attr]
        if w["missing"]:
            out.append(f"  worktree {w['path']} is missing; `git worktree prune` forgets it")
        elif w["dirty"] or w["detached_unique"]:
            out.append(f"  LOSE worktree {w['path']}: {w['dirty']} uncommitted path(s)"
                       + (f", {w['detached_unique']} unique detached commit(s)" if w["detached_unique"] else ""))
    flying = report["tasks_in_flight"]
    out.append("tasks in flight: " + ("  ".join(f"{k}={v}" for k, v in sorted(flying.items())) or "none")  # type: ignore[union-attr]
               + ("" if report["draining"] or not flying else "  (run `workboard drain` and let them land)"))
    waiting = report["tasks_waiting"]
    if waiting:
        out.append("tasks waiting: " + "  ".join(f"{k}={v}" for k, v in sorted(waiting.items()))  # type: ignore[union-attr]
                   + "  (their definitions are in git; their history is in the database)")
    out.append("copy after the drain, with workers stopped:")
    out += [f"  {p['path']}  {p['bytes'] / 2**20:.1f} MiB  ({p['holds']})" for p in report["state_paths"]]  # type: ignore[union-attr]
    out.append("nothing unique would be lost" if not report["would_lose"] else
               "work above marked LOSE exists only on this machine: land it, push it or run `workboard move-archive --apply`")
    return out


def _snapshot_worktree(wt: Path, max_bytes: int) -> tuple[str, list[str]]:
    """Commit a dirty worktree's changes on a scratch index without touching its files or index."""
    skipped: list[str] = []
    keep: list[str] = []
    untracked = gitops.git(wt, "ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0")
    for rel in (p for p in untracked if p):
        try:
            (skipped if (wt / rel).stat().st_size > max_bytes else keep).append(rel)
        except OSError:
            continue
    with tempfile.TemporaryDirectory(prefix="wb-archive-index-") as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if not env.get("GIT_AUTHOR_NAME") and not gitops.git(wt, "config", "user.name").stdout.strip():
            lines_id = gitops.git(wt, "log", "-1", "--format=%an%n%ae", "HEAD").stdout.strip().splitlines()
            if len(lines_id) == 2:
                env.update({"GIT_AUTHOR_NAME": lines_id[0], "GIT_AUTHOR_EMAIL": lines_id[1],
                            "GIT_COMMITTER_NAME": lines_id[0], "GIT_COMMITTER_EMAIL": lines_id[1]})
        subprocess.run(["git", "-C", str(wt), "read-tree", "HEAD"], env=env, check=True, capture_output=True)
        subprocess.run(["git", "-C", str(wt), "add", "--update", "--", "."], env=env, check=True, capture_output=True)
        if keep:
            spec = Path(tmp) / "paths"
            spec.write_bytes(b"\0".join(p.encode() for p in keep))
            subprocess.run(["git", "-C", str(wt), "add", "--pathspec-from-file", str(spec), "--pathspec-file-nul"],
                           env=env, check=True, capture_output=True)
        tree = subprocess.run(["git", "-C", str(wt), "write-tree"], env=env, check=True,
                              capture_output=True, text=True).stdout.strip()
        head = gitops.head(wt)
        msg = (f"Snapshot of uncommitted work in {wt.name}\n\n"
               f"Taken before moving machines. Untracked files over the size cap: {len(skipped)}.\n")
        commit = subprocess.run(["git", "-C", str(wt), "commit-tree", tree, "-p", head, "-m", msg],
                                env=env, check=True, capture_output=True, text=True).stdout.strip()
    return commit, skipped


def archive(cfg: Config, conn: sqlite3.Connection, *, apply: bool = False, date: str | None = None,
            max_file_mb: int = 50) -> dict[str, object]:
    """Snapshot dirty worktrees and push every unique ref and stash to `refs/archive/<date>/` on the remote."""
    repo = cfg.repo
    day = date or datetime.now().strftime("%Y-%m-%d")
    prefix = f"refs/archive/{day}/"
    max_bytes = max_file_mb * 1_000_000
    gitops.fetch(cfg)

    used_labels: set[str] = set()
    snapshots: list[dict[str, object]] = []
    skipped_files: list[str] = []
    for block in gitops.out(repo, "worktree", "list", "--porcelain").split("\n\n"):
        fields = dict(line.split(" ", 1) if " " in line else (line, "") for line in block.splitlines())
        raw_path = fields.get("worktree")
        if not raw_path or not Path(raw_path).is_dir():
            continue
        wt = Path(raw_path)
        if not gitops.dirty(wt):
            continue
        label = wt.name if wt.name not in used_labels else f"{wt.parent.name}-{wt.name}"
        used_labels.add(label)
        ref = f"refs/recovery/{day}/worktrees/{label}"
        if apply:
            commit, skipped = _snapshot_worktree(wt, max_bytes)
            gitops.out(repo, "update-ref", ref, commit)
            skipped_files.extend(f"{wt / p}" for p in skipped)
            snapshots.append({"worktree": str(wt), "ref": ref, "sha": commit})
        else:
            snapshots.append({"worktree": str(wt), "ref": ref, "sha": None})

    candidates: list[dict[str, str]] = []
    for line in gitops.out(repo, "for-each-ref", "--format=%(objectname) %(refname)").splitlines():
        sha, ref = line.split(" ", 1)
        if ref.startswith(ARCHIVE_SKIP_PREFIXES) or ref == f"refs/heads/{cfg.main}":
            continue
        name = ref[len("refs/"):]
        ahead, unique = _unique(cfg, repo, ref)
        if not ahead:
            continue
        if not name.startswith("recovery/") and not unique:
            continue
        candidates.append({"name": name, "sha": sha})
    for idx, sha in enumerate(gitops.git(repo, "stash", "list", "--format=%H").stdout.split()):
        candidates.append({"name": f"stash/{idx}", "sha": sha})

    if not apply:
        return {"applied": False, "prefix": prefix, "snapshots": snapshots, "candidates": candidates,
                "pushed": 0, "confirmed": 0, "missing": [], "skipped_files": skipped_files,
                "ok": not snapshots and not candidates}

    existing = {ref: sha for sha, ref in (line.split("\t", 1) for line in
                gitops.git(repo, "ls-remote", cfg.remote, f"{prefix}*").stdout.splitlines() if "\t" in line)}
    stamp = datetime.now().strftime("t%H%M%S")
    pushes: dict[str, str] = {}
    for cand in candidates:
        dest = prefix + cand["name"]
        if existing.get(dest) == cand["sha"]:
            continue
        pushes[f"{dest}-{stamp}" if dest in existing else dest] = cand["sha"]

    items = sorted(pushes.items())
    for start in range(0, len(items), 50):
        chunk = items[start:start + 50]
        res = gitops.git(repo, "push", "--quiet", cfg.remote, *[f"{sha}:{dest}" for dest, sha in chunk])
        if res.returncode != 0:
            raise RuntimeError(f"archive push failed: {(res.stderr or res.stdout).strip()}")

    remote_after = {ref: sha for sha, ref in (line.split("\t", 1) for line in
                    gitops.git(repo, "ls-remote", cfg.remote, f"{prefix}*").stdout.splitlines() if "\t" in line)}
    confirmed_shas = set(remote_after.values())
    missing = [c["name"] for c in candidates if c["sha"] not in confirmed_shas]
    with store.write(conn):
        store.event(conn, "cli", "move_archive", None, f"{len(pushes)} pushed under {prefix}")
    return {"applied": True, "prefix": prefix, "snapshots": snapshots, "candidates": candidates,
            "pushed": len(pushes), "confirmed": len(candidates) - len(missing),
            "missing": missing, "skipped_files": skipped_files,
            "ok": not missing and not skipped_files}


def archive_lines(result: dict[str, object]) -> list[str]:
    out: list[str] = []
    for snap in result["snapshots"]:  # type: ignore[union-attr]
        verb = "snapshot" if result["applied"] else "would snapshot"
        out.append(f"{verb}  {snap['worktree']} -> {snap['ref']}")
    if not result["applied"]:
        for cand in result["candidates"]:  # type: ignore[union-attr]
            out.append(f"would archive  {result['prefix']}{cand['name']}  ({str(cand['sha'])[:10]})")
        if not out:
            out.append("nothing unique to archive")
        else:
            out.append("re-run with `--apply` to snapshot dirty worktrees, push to the remote and verify")
        return out
    out.append(f"archived {result['pushed']} new ref(s) under {result['prefix']}; "
               f"{result['confirmed']} of {len(result['candidates'])} confirmed on the remote")  # type: ignore[arg-type]
    for path in result["skipped_files"]:  # type: ignore[union-attr]
        out.append(f"  NOT IN GIT (over size cap, copy by hand): {path}")
    for name in result["missing"]:  # type: ignore[union-attr]
        out.append(f"  NOT CONFIRMED on remote: {name}")
    return out
