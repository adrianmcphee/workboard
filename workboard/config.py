"""Load `workboard.toml`: the one file that describes a project to Workboard."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import tomllib

CONFIG_NAME = "workboard.toml"


@dataclass
class Harness:
    name: str
    command: list[str]
    prompt: str = "stdin"            # stdin | arg | file
    model: str = ""
    model_flag: str = ""
    slots: int = 1
    quota_patterns: list[str] = field(default_factory=list)
    cooldown_min: int = 30
    usage: str = "none"              # none | claude-code | codex | command
    usage_command: list[str] = field(default_factory=list)
    env_unset: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Config:
    root: Path
    name: str = "project"
    repo: Path = Path(".")
    remote: str = "origin"
    main: str = "main"
    worktrees: Path = Path(".worktrees")
    state: Path = Path(".workboard")
    tasks: Path = Path("workboard/tasks")
    rules: list[Path] = field(default_factory=list)
    branch_prefix: str = "workboard/"
    max_coding: int = 4
    max_attempts: int = 6
    max_load: float = 0.0
    min_free_gib: float = 10.0
    heartbeat_s: int = 20
    heartbeat_ttl_s: int = 90
    default_lease_min: int = 60
    review_lease_min: int = 20
    review_required: bool = True
    cross_harness: bool = True
    review_fallback_s: int = 180
    guards: list[str] = field(default_factory=list)
    land_command: str = ""
    build_command: str = ""
    build_schedule: list[tuple[int, int]] = field(default_factory=list)
    build_cutoff_min: int = 30
    usage_policy: str = "enforce"    # off | advise | enforce
    usage_reserve_pct: float = 3.0
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8787
    harnesses: dict[str, Harness] = field(default_factory=dict)

    @property
    def remote_main(self) -> str:
        return f"{self.remote}/{self.main}"

    @property
    def db(self) -> Path:
        return self.state / "workboard.db"

    def handoff(self, task_id: str) -> Path:
        path = self.state / "handoffs" / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def worktree(self, task_id: str) -> Path:
        return self.worktrees / task_id


class ConfigError(Exception):
    pass


def find_root(start: Path | None = None) -> Path:
    env = os.environ.get("WORKBOARD_ROOT")
    if env:
        return Path(env).resolve()
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        if (folder / CONFIG_NAME).is_file():
            return folder
    raise ConfigError(f"no {CONFIG_NAME} found here or in any parent directory; run `workboard init`")


def _slot(value: str) -> tuple[int, int]:
    hour, _, minute = value.partition(":")
    slot = (int(hour), int(minute or 0))
    if not (0 <= slot[0] <= 23 and 0 <= slot[1] <= 59):
        raise ConfigError(f"invalid build time {value!r}")
    return slot


def load(root: Path | None = None) -> Config:
    root = (root or find_root()).resolve()
    path = root / CONFIG_NAME
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    project, limits = data.get("project", {}), data.get("limits", {})
    review, build, usage = data.get("review", {}), data.get("build", {}), data.get("usage", {})
    dash = data.get("dashboard", {})

    def rel(value: str) -> Path:
        candidate = Path(os.path.expanduser(str(value)))
        return candidate if candidate.is_absolute() else (root / candidate).resolve()

    cfg = Config(
        root=root,
        name=str(project.get("name", root.name)),
        repo=rel(project.get("repo", ".")),
        remote=str(project.get("remote", "origin")),
        main=str(project.get("main", "main")),
        worktrees=rel(project.get("worktrees", ".worktrees")),
        state=rel(project.get("state", ".workboard")),
        tasks=rel(project.get("tasks", "workboard/tasks")),
        rules=[rel(p) for p in project.get("rules", [])],
        branch_prefix=str(project.get("branch_prefix", "workboard/")),
        max_coding=int(limits.get("max_coding", 4)),
        max_attempts=int(limits.get("max_attempts", 6)),
        max_load=float(limits.get("max_load", 0)),
        min_free_gib=float(limits.get("min_free_gib", 10)),
        heartbeat_s=int(limits.get("heartbeat_s", 20)),
        heartbeat_ttl_s=int(limits.get("heartbeat_ttl_s", 90)),
        default_lease_min=int(limits.get("default_lease_min", 60)),
        review_lease_min=int(limits.get("review_lease_min", 20)),
        review_required=bool(review.get("required", True)),
        cross_harness=bool(review.get("cross_harness", True)),
        review_fallback_s=int(review.get("fallback_after_s", 180)),
        guards=[str(c) for c in data.get("guards", {}).get("commands", [])],
        land_command=str(data.get("land", {}).get("command", "")),
        build_command=str(build.get("command", "")),
        build_schedule=sorted(_slot(str(s)) for s in build.get("schedule", [])),
        build_cutoff_min=int(build.get("cutoff_min", 30)),
        usage_policy=str(usage.get("policy", "enforce")),
        usage_reserve_pct=float(usage.get("reserve_pct", 3)),
        dashboard_host=str(dash.get("host", "127.0.0.1")),
        dashboard_port=int(dash.get("port", 8787)),
    )
    if cfg.usage_policy not in ("off", "advise", "enforce"):
        raise ConfigError("[usage] policy must be off, advise or enforce")
    for name, raw in data.get("harness", {}).items():
        command = raw.get("command")
        if not isinstance(command, list) or not command:
            raise ConfigError(f"[harness.{name}] command must be a non-empty list")
        harness = Harness(
            name=name, command=[str(c) for c in command], prompt=str(raw.get("prompt", "stdin")),
            model=str(raw.get("model", "")), model_flag=str(raw.get("model_flag", "")),
            slots=int(raw.get("slots", 1)), quota_patterns=[str(p) for p in raw.get("quota_patterns", [])],
            cooldown_min=int(raw.get("cooldown_min", 30)), usage=str(raw.get("usage", "none")),
            usage_command=[str(c) for c in raw.get("usage_command", [])],
            env_unset=[str(e) for e in raw.get("env_unset", [])],
            env={str(k): str(v) for k, v in raw.get("env", {}).items()},
        )
        if harness.prompt not in ("stdin", "arg", "file"):
            raise ConfigError(f"[harness.{name}] prompt must be stdin, arg or file")
        if harness.usage not in ("none", "claude-code", "codex", "command"):
            raise ConfigError(f"[harness.{name}] usage must be none, claude-code, codex or command")
        cfg.harnesses[name] = harness
    return cfg
