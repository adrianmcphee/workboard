"""The scheduled build as a train: which build each slice is going for.

A build is made from whatever is on main at its scheduled minute. A slice rides the first build
whose land cutoff falls after the slice is expected to be on main.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import sqlite3

from .config import Config

DEFAULT_AUTHOR_MIN = 35.0


def build_id(when: datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M")


def upcoming(cfg: Config, now: datetime, count: int = 3) -> list[datetime]:
    slots = []
    if not cfg.build_schedule:
        return slots
    for day in range(count + 2):
        for hour, minute in cfg.build_schedule:
            slot = (now + timedelta(days=day)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            if slot > now:
                slots.append(slot)
    return sorted(slots)[:count]


def target_for(cfg: Config, on_main_at: datetime, now: datetime) -> datetime | None:
    margin = timedelta(minutes=cfg.build_cutoff_min)
    builds = upcoming(cfg, now, count=8)
    for build in builds:
        if on_main_at <= build - margin:
            return build
    return builds[-1] if builds else None


def author_minutes(conn: sqlite3.Connection, harness: str | None) -> float:
    """Observed mean time from claim to ready-for-review, for one harness when it has history."""
    sql = "SELECT AVG((ended_at-started_at)/60.0) FROM attempts WHERE role='author' AND outcome='ready'"
    row = conn.execute(sql + (" AND harness=?" if harness else ""), (harness,) if harness else ()).fetchone()
    return float(row[0]) if row and row[0] else DEFAULT_AUTHOR_MIN


def expected_on_main(cfg: Config, status: str, claimed_at: float | None, now: datetime, author_min: float) -> datetime:
    allowance = timedelta(minutes=cfg.build_cutoff_min)
    if status == "LANDING":
        return now
    if status in ("AWAITING_REVIEW", "REVIEWING"):
        return now + allowance
    started = datetime.fromtimestamp(claimed_at) if claimed_at else now
    return max(now, started + timedelta(minutes=author_min)) + allowance


def manifest(conn: sqlite3.Connection, cfg: Config, now: datetime) -> dict[str, object]:
    """Upcoming builds, each with the in-flight slices expected aboard and their slack to the cutoff."""
    builds = upcoming(cfg, now)
    margin = timedelta(minutes=cfg.build_cutoff_min)
    aboard: dict[str, list[dict[str, object]]] = {build_id(b): [] for b in builds}
    minutes: dict[str | None, float] = {}
    rows = conn.execute("SELECT task_id, title, status, lease_harness, claimed_at FROM tasks WHERE status IN "
                        "('CLAIMED','AWAITING_REVIEW','REVIEWING','LANDING','NEEDS_TAKEOVER')").fetchall() if builds else []
    for row in rows:
        harness = None if row["status"] == "NEEDS_TAKEOVER" else row["lease_harness"]
        claimed = None if row["status"] == "NEEDS_TAKEOVER" else row["claimed_at"]
        if harness not in minutes:
            minutes[harness] = author_minutes(conn, harness)
        ready = expected_on_main(cfg, row["status"], claimed, now, minutes[harness])
        target = target_for(cfg, ready, now)
        if target is None:
            continue
        aboard.setdefault(build_id(target), []).append({
            "task_id": row["task_id"], "title": row["title"], "status": row["status"],
            "expected_on_main": ready.strftime("%H:%M"),
            "slack_min": round((target - margin - ready).total_seconds() / 60.0)})
    return {"cutoff_min": cfg.build_cutoff_min, "builds": [{
        "build": build_id(b), "label": b.strftime("%a %H:%M"),
        "minutes_away": round((b - now).total_seconds() / 60.0), "cutoff": (b - margin).strftime("%H:%M"),
        "minutes_to_cutoff": round((b - margin - now).total_seconds() / 60.0),
        "tasks": sorted(aboard.get(build_id(b), []), key=lambda t: t["slack_min"])} for b in builds]}
