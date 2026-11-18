"""Subscription usage per harness: allowance left, reset time, burn rate and pace.

Readers use only what an agent CLI already caches on disk, or a command you supply. Nothing here
calls a provider or reads a credential, and nothing raises into a caller: unknown or stale usage
means "no opinion", so work continues.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import time

from .config import Config, Harness

STALE_S = 3 * 3600.0
RATE_LOOKBACK_S = 6 * 3600.0
RATE_MIN_SPAN_S = 20 * 60.0
OVER_PCT = 100.0
UNDER_PCT = 85.0
_RANK = {"reserve": 4, "over": 3, "on": 2, "under": 1}


def _epoch(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def window(label: str, used: object, resets_at: object, length_s: float | None = None,
           started_at: object = None, binding: bool = True) -> dict[str, object] | None:
    if not isinstance(used, (int, float)):
        return None
    reset, start = _epoch(resets_at), _epoch(started_at)
    if start is None and reset is not None and length_s:
        start = reset - length_s
    return {"label": label, "used_pct": round(float(used), 1), "remaining_pct": round(max(0.0, 100.0 - float(used)), 1),
            "resets_at": reset, "started_at": start, "binding": binding}


def read_claude_code(home: Path) -> dict[str, object]:
    out: dict[str, object] = {"source": "~/.claude.json usage cache", "windows": [], "extras": [], "fetched_at": None}
    try:
        data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {**out, "error": "usage cache unreadable"}
    cache = data.get("cachedUsageUtilization") or {}
    util = cache.get("utilization") or {}
    fetched = cache.get("fetchedAtMs")
    out["fetched_at"] = float(fetched) / 1000.0 if isinstance(fetched, (int, float)) else None
    week_start = (util.get("seven_day_breakdown") or {}).get("window_started_at")
    windows = []
    for lim in util.get("limits") or []:
        if not isinstance(lim, dict):
            continue
        weekly = lim.get("group") == "weekly"
        label = "weekly" if lim.get("kind") == "weekly_all" else str(lim.get("kind") or "limit")
        model = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
        if model:
            label = f"weekly {model}"
        win = window(label, lim.get("percent"), lim.get("resets_at"), 7 * 86400.0 if weekly else None,
                     week_start if weekly else None, lim.get("kind") == "weekly_all" or bool(lim.get("is_active")))
        if win:
            windows.append(win)
    if not windows:
        seven = util.get("seven_day") or {}
        win = window("weekly", seven.get("utilization"), seven.get("resets_at"), 7 * 86400.0, week_start)
        if win:
            windows.append(win)
    five = util.get("five_hour") or {}
    win = window("5h", five.get("utilization"), five.get("resets_at"), 5 * 3600.0)
    if win:
        windows.append(win)
    extra = util.get("extra_usage") or {}
    out["windows"] = windows
    out["extras"] = ["extra usage on" if extra.get("is_enabled") else "extra usage off"]
    if not windows:
        out["error"] = "no usage windows in cache"
    return out


def _last_rate_limits(path: Path, tail_bytes: int = 400_000) -> dict[str, object] | None:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - tail_bytes))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    key, decoder = '"rate_limits":', json.JSONDecoder()
    pos = text.rfind(key)
    while pos >= 0:
        try:
            obj, _ = decoder.raw_decode(text[pos + len(key):].lstrip())
        except ValueError:
            obj = None
        if isinstance(obj, dict) and (obj.get("primary") or obj.get("secondary")):
            return obj
        pos = text.rfind(key, 0, pos)
    return None


def read_codex(home: Path) -> dict[str, object]:
    out: dict[str, object] = {"source": "~/.codex/sessions rate limits", "windows": [], "extras": [], "fetched_at": None}
    root = home / ".codex" / "sessions"
    try:
        days = sorted((p for p in root.glob("*/*/*") if p.is_dir()), reverse=True)[:3]
        files = sorted((f for d in days for f in d.glob("*.jsonl")), key=lambda f: f.stat().st_mtime, reverse=True)[:6]
    except OSError:
        files = []
    for path in files:
        limits = _last_rate_limits(path)
        if not limits:
            continue
        fetched = path.stat().st_mtime
        windows = []
        for slot in ("primary", "secondary"):
            lim = limits.get(slot)
            if not isinstance(lim, dict):
                continue
            minutes = lim.get("window_minutes")
            reset = lim.get("resets_at")
            if reset is None and isinstance(lim.get("resets_in_seconds"), (int, float)):
                reset = fetched + float(lim["resets_in_seconds"])
            label = "weekly" if minutes == 10080 else "5h" if minutes == 300 else f"{minutes}m"
            win = window(label, lim.get("used_percent"), reset,
                         float(minutes) * 60.0 if isinstance(minutes, (int, float)) else None)
            if win:
                windows.append(win)
        credits = limits.get("credits") or {}
        out.update({"windows": windows, "fetched_at": fetched, "plan": limits.get("plan_type"),
                    "extras": ["credits available" if credits.get("has_credits") or credits.get("unlimited") else "no credits"]})
        return out
    return {**out, "error": "no recent session with rate limits"}


def read_command(harness: Harness, now: float) -> dict[str, object]:
    """Run the configured command; it prints JSON such as
    {"windows": [{"label": "weekly", "used_pct": 40, "resets_at": "2030-01-07T00:00:00Z", "window_hours": 168}]}"""
    out: dict[str, object] = {"source": "usage_command", "windows": [], "extras": [], "fetched_at": None}
    try:
        result = subprocess.run(harness.usage_command, capture_output=True, text=True, timeout=30, check=False)
        data = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {**out, "error": f"usage command failed: {exc}"[:200]}
    windows = []
    for raw in data.get("windows", []) if isinstance(data, dict) else []:
        hours = raw.get("window_hours")
        win = window(str(raw.get("label", "window")), raw.get("used_pct"), raw.get("resets_at"),
                     float(hours) * 3600.0 if isinstance(hours, (int, float)) else None)
        if win:
            windows.append(win)
    out.update({"windows": windows, "fetched_at": now, "extras": [str(e) for e in data.get("extras", [])]})
    if not windows:
        out["error"] = "usage command returned no windows"
    return out


def project(harness: str, win: dict[str, object], fetched_at: float | None, samples: list[dict[str, object]],
            now: float, reserve_pct: float) -> None:
    """Annotate a window with its burn rate, projected use at reset and pace."""
    reset = win.get("resets_at")
    used = float(win["used_pct"])  # type: ignore[arg-type]
    as_of = fetched_at or now
    win.update({"rate_pct_per_h": None, "rate_basis": None, "projected_pct_at_reset": None,
                "exhausts_at": None, "hours_to_reset": None, "pace": "unknown"})
    if not isinstance(reset, (int, float)) or reset <= now:
        return
    win["hours_to_reset"] = round((reset - now) / 3600.0, 1)
    rate = basis = None
    same = [s for s in samples if s.get("harness") == harness and s.get("label") == win["label"]
            and isinstance(s.get("resets_at"), (int, float)) and abs(float(s["resets_at"]) - reset) < 3600
            and as_of - RATE_LOOKBACK_S <= float(s["t"]) < as_of]
    if same:
        first = min(same, key=lambda s: float(s["t"]))
        span = as_of - float(first["t"])
        if span >= RATE_MIN_SPAN_S and used >= float(first["used_pct"]):
            rate, basis = (used - float(first["used_pct"])) / (span / 3600.0), f"last {span / 3600.0:.1f}h"
    if rate is None:
        start = win.get("started_at")
        if isinstance(start, (int, float)) and as_of - start >= RATE_MIN_SPAN_S:
            rate, basis = used / ((as_of - start) / 3600.0), "average since window start"
    if rate is None:
        return
    projected = used + rate * (reset - as_of) / 3600.0
    win.update({"rate_pct_per_h": round(rate, 2), "rate_basis": basis, "projected_pct_at_reset": round(projected, 1)})
    if rate > 0 and projected > 100.0:
        win["exhausts_at"] = as_of + (100.0 - used) / rate * 3600.0
    if float(win["remaining_pct"]) <= reserve_pct:  # type: ignore[arg-type]
        win["pace"] = "reserve"
    elif projected > OVER_PCT:
        win["pace"] = "over"
    elif projected < UNDER_PCT:
        win["pace"] = "under"
    else:
        win["pace"] = "on"


def _samples(cfg: Config, now: float) -> list[dict[str, object]]:
    rows = []
    try:
        for line in (cfg.state / "usage-samples.jsonl").read_text(encoding="utf-8").splitlines()[-5000:]:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and now - float(row.get("t", 0)) <= 8 * 86400:
                rows.append(row)
    except OSError:
        pass
    return rows


def collect(cfg: Config, now: float | None = None, home: Path | None = None, record: bool = True) -> dict[str, object]:
    now = now or time.time()
    home = home or Path.home()
    samples = _samples(cfg, now)
    seen = {(s.get("harness"), s.get("label"), s.get("t")) for s in samples}
    fresh, entries = [], []
    for name, harness in cfg.harnesses.items():
        try:
            if harness.usage == "claude-code":
                entry = read_claude_code(home)
            elif harness.usage == "codex":
                entry = read_codex(home)
            elif harness.usage == "command":
                entry = read_command(harness, now)
            else:
                entry = {"source": "none", "windows": [], "extras": [], "fetched_at": None,
                         "error": "no usage reader configured"}
        except Exception as exc:
            entry = {"source": "error", "windows": [], "extras": [], "fetched_at": None, "error": str(exc)[:160]}
        entry["harness"] = name
        fetched = entry.get("fetched_at")
        age = now - float(fetched) if isinstance(fetched, (int, float)) else None
        entry["age_s"] = round(age) if age is not None else None
        entry["stale"] = age is None or age > STALE_S
        for win in entry["windows"]:  # type: ignore[union-attr]
            if isinstance(fetched, (int, float)) and (name, win["label"], fetched) not in seen:
                fresh.append({"t": fetched, "harness": name, "label": win["label"], "used_pct": win["used_pct"],
                              "resets_at": win["resets_at"]})
            project(name, win, fetched if isinstance(fetched, (int, float)) else None, samples, now, cfg.usage_reserve_pct)
        binding = [w for w in entry["windows"] if w.get("binding")]  # type: ignore[union-attr]
        worst = max(binding, key=lambda w: (_RANK.get(str(w["pace"]), 0), float(w["used_pct"])), default=None)
        entry["pace"] = str(worst["pace"]) if worst else "unknown"
        entry["binding_window"] = worst["label"] if worst else None
        entries.append(entry)
    if record and fresh:
        try:
            cfg.state.mkdir(parents=True, exist_ok=True)
            with open(cfg.state / "usage-samples.jsonl", "a", encoding="utf-8") as fh:
                for row in fresh:
                    fh.write(json.dumps(row) + "\n")
        except OSError:
            pass
    return {"generated_at": now, "policy": cfg.usage_policy, "reserve_pct": cfg.usage_reserve_pct, "harnesses": entries}


def snapshot(cfg: Config, max_age_s: float = 60.0, now: float | None = None) -> dict[str, object]:
    """collect(), cached for a minute so a claim never re-reads provider files more often than that."""
    now = now or time.time()
    path = cfg.state / "usage-latest.json"
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if now - float(cached.get("generated_at", 0)) <= max_age_s:
            return cached
    except (OSError, ValueError, TypeError):
        pass
    snap = collect(cfg, now=now)
    try:
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(snap), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    return snap


def entry_for(snap: dict[str, object], harness: str) -> dict[str, object] | None:
    return next((e for e in snap.get("harnesses", []) if e.get("harness") == harness), None)  # type: ignore[union-attr]


def binding(entry: dict[str, object] | None) -> dict[str, object] | None:
    if not entry:
        return None
    return next((w for w in entry["windows"] if w["label"] == entry.get("binding_window")), None)  # type: ignore[union-attr]


def claim_verdict(snap: dict[str, object], harness: str, active_coding: int, slots: int,
                  live: set[str]) -> dict[str, object]:
    """May this harness take new authoring work?

    The automation never lowers total throughput on its own. It stops a harness only at its reserve,
    and makes an over-pace harness yield only while another running harness has allowance that would
    otherwise expire unused. Every other trade-off is left to a human as a potential intervention.
    """
    allow = {"allow": True, "reason": "ok"}
    me = entry_for(snap, harness)
    if snap.get("policy") != "enforce" or not me or me.get("stale") or me.get("pace") in (None, "unknown"):
        return allow
    win = binding(me)
    if not win:
        return allow
    now = float(snap.get("generated_at") or time.time())
    if me["pace"] == "reserve":
        return {"allow": False, "reason": "subscription_reserve", "remaining_pct": win["remaining_pct"],
                "retry_after_s": max(60, int(float(win["resets_at"]) - now))}
    if me["pace"] != "over":
        return allow
    donors = sorted(h for h in live - {harness}
                    if (entry_for(snap, h) or {}).get("pace") == "under" and not (entry_for(snap, h) or {}).get("stale"))
    if not donors:
        return allow
    rate, hours = float(win.get("rate_pct_per_h") or 0.0), float(win.get("hours_to_reset") or 0.0)
    sustainable = float(win["remaining_pct"]) / hours if hours > 0 else 0.0
    paced = max(1, int(max(1, slots) * sustainable / rate)) if rate > 0 else max(1, slots)
    if active_coding < paced:
        return allow
    return {"allow": False, "reason": "subscription_pace_yield", "paced_slots": paced, "yield_to": donors,
            "retry_after_s": 120}


def when(epoch: object) -> str:
    return datetime.fromtimestamp(float(epoch)).strftime("%a %d %b %H:%M") if isinstance(epoch, (int, float)) else "unknown"


def lines(snap: dict[str, object]) -> list[str]:
    out = []
    for entry in snap.get("harnesses", []):  # type: ignore[union-attr]
        name = str(entry["harness"])
        shown = [w for w in entry["windows"] if w.get("binding")]
        if not shown:
            out.append(f"{name}: usage unknown ({entry.get('error') or 'no data'})")
            continue
        for win in shown:
            text = f"{name} {win['label']}: {win['remaining_pct']:.0f}% left, resets {when(win['resets_at'])}"
            if win.get("exhausts_at"):
                text += f", burning {win['rate_pct_per_h']}%/h, runs out {when(win['exhausts_at'])}"
            elif win.get("projected_pct_at_reset") is not None:
                text += f", burning {win['rate_pct_per_h']}%/h, {max(0.0, 100 - float(win['projected_pct_at_reset'])):.0f}% unused at reset"
            out.append(text + f" [{win['pace']}{', stale' if entry.get('stale') else ''}]")
    return out
