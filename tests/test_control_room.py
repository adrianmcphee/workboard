"""Usage, pace, the build train, builds with sign-off, interventions and the dashboard."""
from __future__ import annotations

from datetime import datetime
import json
import os
import time
import threading
import unittest
import urllib.error
import urllib.request

from workboard import builds, control, dashboard, interventions, prompt, queue, store, train, usage
from tests.support import ProjectCase, sh

NOW, HOUR = 1_800_000_000.0, 3600.0


def entry(harness: str, used: float, hours_left: float, age_s: float = 60.0) -> dict[str, object]:
    reset = NOW + hours_left * HOUR
    win = usage.window("weekly", used, reset, 168 * HOUR)
    usage.project(harness, win, NOW, [], NOW, 3.0)
    return {"harness": harness, "windows": [win], "extras": [], "fetched_at": NOW - age_s, "age_s": age_s,
            "stale": age_s > usage.STALE_S, "pace": win["pace"], "binding_window": "weekly"}


def snap(*entries: dict[str, object], policy: str = "enforce") -> dict[str, object]:
    return {"generated_at": NOW, "policy": policy, "reserve_pct": 3.0, "harnesses": list(entries)}


class Usage(unittest.TestCase):
    def test_pace_follows_projected_use_at_reset(self) -> None:
        self.assertEqual(entry("a", 51, 145)["pace"], "over")
        self.assertEqual(entry("a", 30, 24)["pace"], "under")
        self.assertEqual(entry("a", 80, 30)["pace"], "on")
        self.assertEqual(entry("a", 98, 30)["pace"], "reserve")
        over = entry("a", 51, 145)["windows"][0]
        self.assertLess(over["exhausts_at"], over["resets_at"])

    def test_recent_readings_override_the_window_average(self) -> None:
        win = usage.window("weekly", 50, NOW + 84 * HOUR, 168 * HOUR)
        usage.project("a", win, NOW, [{"t": NOW - 2 * HOUR, "harness": "a", "label": "weekly", "used_pct": 40,
                                       "resets_at": win["resets_at"]}], NOW, 3.0)
        self.assertEqual((win["rate_pct_per_h"], win["pace"]), (5.0, "over"))

    def test_verdict_stops_at_reserve_and_yields_only_to_a_live_harness_with_spare_allowance(self) -> None:
        over, under = entry("a", 51, 145), entry("b", 30, 24)
        self.assertEqual(usage.claim_verdict(snap(entry("a", 98, 30)), "a", 0, 2, {"a"})["reason"], "subscription_reserve")
        self.assertEqual(usage.claim_verdict(snap(over, under), "a", 1, 3, {"a", "b"})["reason"], "subscription_pace_yield")
        self.assertTrue(usage.claim_verdict(snap(over, under), "a", 0, 3, {"a", "b"})["allow"])
        self.assertTrue(usage.claim_verdict(snap(over, under), "a", 1, 3, {"a"})["allow"])
        self.assertTrue(usage.claim_verdict(snap(over, entry("b", 90, 100)), "a", 3, 3, {"a", "b"})["allow"])

    def test_stale_unknown_or_advisory_usage_never_blocks(self) -> None:
        self.assertTrue(usage.claim_verdict(snap(entry("a", 99, 30, age_s=usage.STALE_S + 1)), "a", 9, 1, {"a"})["allow"])
        self.assertTrue(usage.claim_verdict(snap(), "a", 9, 1, {"a"})["allow"])
        self.assertTrue(usage.claim_verdict(snap(entry("a", 99, 30), policy="advise"), "a", 9, 1, {"a"})["allow"])


class UsageReaders(ProjectCase):
    def test_cli_caches_and_a_command_are_read_and_missing_data_is_unknown(self) -> None:
        home = self.tmp / "home"
        (home / ".codex/sessions/2030/01/15").mkdir(parents=True)
        (home / ".claude.json").write_text(json.dumps({"cachedUsageUtilization": {"fetchedAtMs": NOW * 1000, "utilization": {
            "limits": [{"kind": "weekly_all", "group": "weekly", "percent": 80, "resets_at": "2030-01-17T00:00:00+00:00"}]}}}))
        (home / ".codex/sessions/2030/01/15/rollout.jsonl").write_text(json.dumps({"rate_limits": {
            "primary": {"used_percent": 51.0, "window_minutes": 10080, "resets_at": NOW + 100 * HOUR}, "plan_type": "pro"}}) + "\n")
        self.assertEqual(usage.read_claude_code(home)["windows"][0]["remaining_pct"], 20.0)
        self.assertEqual((usage.read_codex(home)["plan"], usage.read_codex(home)["windows"][0]["label"]), ("pro", "weekly"))
        self.cfg.harnesses["alpha"].usage = "command"
        self.cfg.harnesses["alpha"].usage_command = ["sh", "-c", f"echo '{{\"windows\":[{{\"label\":\"daily\",\"used_pct\":10,\"resets_at\":{time.time() + 3600},\"window_hours\":24}}]}}'"]
        got = usage.collect(self.cfg, home=self.tmp / "nowhere")
        by = {e["harness"]: e for e in got["harnesses"]}
        self.assertEqual(by["alpha"]["windows"][0]["label"], "daily")
        self.assertEqual((by["beta"]["pace"], by["beta"]["stale"]), ("unknown", True))


class TrainAndPrompt(ProjectCase):
    def test_slice_rides_the_first_build_whose_cutoff_it_makes(self) -> None:
        now = datetime(2030, 1, 15, 11, 0)
        self.assertEqual(train.target_for(self.cfg, datetime(2030, 1, 15, 12, 0), now), datetime(2030, 1, 15, 12, 30))
        self.assertEqual(train.target_for(self.cfg, datetime(2030, 1, 15, 12, 1), now), datetime(2030, 1, 15, 20, 30))
        self.assertEqual(train.target_for(self.cfg, datetime(2030, 1, 15, 20, 10), now), datetime(2030, 1, 16, 12, 30))

    def test_prompt_names_the_build_the_budget_and_keeps_task_text_literal(self) -> None:
        now = datetime(2030, 1, 15, 9, 0)
        text = prompt.workboard_context(self.cfg, self.conn, "author", "alpha", 60, now=now,
                                    usage_snap=snap(entry("alpha", 51, 145)))
        self.assertIn("build id `2030-01-15 12:30`", text)
        self.assertIn("**Land cutoff:** 12:00", text)
        self.assertIn("49% left", text)
        self.assertIn("ahead of its pace", text)
        self.task("lit", body="Run `$(touch /nope)` and keep {worktree} as written")
        self.sync()
        claim = queue.claim(self.conn, self.cfg, "alpha", "w", 1, alive=lambda _p: True)
        rendered = prompt.render(self.cfg, claim, "alpha", self.cfg.worktree("lit"), self.conn)
        self.assertIn("Run `$(touch /nope)` and keep {worktree} as written", rendered)
        self.assertTrue(rendered.startswith("<!-- workboard role=author task=lit "))
        self.assertNotIn(chr(0x2014), rendered)


class BuildsAndSignoff(ProjectCase):
    def land(self, task_id: str, body: str = "x") -> None:
        self.task(task_id, body=body, paths="[src/a]")
        self.sync()
        self.work("alpha")
        self.work("beta")
        self.assertEqual(self.status(task_id), "DONE")

    def test_build_records_what_landed_and_a_rejection_queues_a_charged_fix(self) -> None:
        self.land("one")
        first = builds.run(self.cfg, self.conn, now=datetime(2030, 1, 15, 12, 31))
        self.assertEqual((first["result"], first["slot"], [s["task_id"] for s in first["slices"]]),
                         ("built", "2030-01-15 12:30", ["one"]))
        self.assertEqual((first["slices"][0]["author"], first["slices"][0]["reviewer"]), ("alpha", "beta"))
        self.assertTrue(builds.run(self.cfg, self.conn).get("unchanged"))
        self.land("two")
        second = builds.run(self.cfg, self.conn, now=datetime(2030, 1, 15, 16, 0))
        self.assertEqual((second["slot"], [s["task_id"] for s in second["slices"]]), ("unscheduled", ["two"]))
        with self.assertRaises(ValueError):
            builds.sign(self.cfg, self.conn, None, "two", False, "  ")
        with self.assertRaises(ValueError):
            builds.sign(self.cfg, self.conn, None, "one", True)  # not in the latest build
        entry_ = builds.sign(self.cfg, self.conn, None, "two", False, "the knob jumps when released")
        fix = self.row(entry_["followup"])
        self.assertEqual((fix["task_id"], fix["status"], fix["priority"], fix["paths_json"]),
                         ("two-fix1", "READY", builds.FOLLOWUP_PRIORITY, '["src/a"]'))
        self.assertIn("the knob jumps when released", open(fix["brief_path"]).read())
        builds.sign(self.cfg, self.conn, str(first["id"]), "one", True, "good")
        self.assertEqual(builds.escapes(self.cfg), {"author": {"alpha": {"accepted": 1, "rejected": 1}},
                                                     "reviewer": {"beta": {"accepted": 1, "rejected": 1}}})
        self.assertEqual(builds.summary(self.cfg)["latest"]["pending"], [])
        self.assertEqual(queue.claim(self.conn, self.cfg, "alpha", "w", 1, alive=lambda _p: True)["task_id"], "two-fix1")


class FailedBuild(ProjectCase):
    build = "false"

    def test_failed_build_lists_suspects_and_is_not_offered_for_signoff(self) -> None:
        self.task("sus")
        self.sync()
        self.work("alpha")
        self.work("beta")
        record = builds.run(self.cfg, self.conn)
        self.assertEqual((record["result"], [s["task_id"] for s in record["slices"]]), ("failed", ["sus"]))
        summary = builds.summary(self.cfg)
        self.assertIsNone(summary["latest"])
        self.assertEqual(summary["failed"]["id"], record["id"])


class Interventions(ProjectCase):
    def test_cards_cover_budget_triage_cancelled_dependencies_and_the_critical_path(self) -> None:
        self.task("root")
        for i in range(3):
            self.task(f"leaf{i}", depends_on="[root]")
        self.task("dead")
        self.task("orphan", depends_on="[dead]")
        self.task("stuck")
        self.sync()
        queue.cancel(self.conn, "dead")
        with store.write(self.conn):
            self.conn.execute("UPDATE tasks SET status='NEEDS_TRIAGE', attempts=6, reason='no commits' WHERE task_id='stuck'")
        cards = interventions.collect(self.cfg, self.conn, snap(entry("alpha", 51, 145), entry("beta", 30, 24)),
                                      {"alpha": 3, "beta": 1}, {"builds": []})
        kinds = {c["kind"]: c for c in cards}
        self.assertEqual(cards[0]["kind"], "triage")
        self.assertEqual({"triage", "usage_over_pace", "usage_under_pace", "blocked_by_cancelled", "critical_path"}, set(kinds))
        self.assertIn("workboard up --harness alpha --slots 1", kinds["usage_over_pace"]["action"])
        self.assertIn("Shift authoring to beta", kinds["usage_over_pace"]["what"])
        self.assertEqual(kinds["usage_over_pace"]["ops"][0]["args"], {"harness": "alpha", "slots": 1})
        self.assertEqual(kinds["critical_path"]["ops"][0]["args"], {"task": "root", "priority": 500})


class Dashboard(ProjectCase):
    def post(self, port: int, body: dict[str, object], headers: dict[str, str]) -> tuple[int, dict[str, object]]:
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/act", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", **headers}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_state_is_served_and_writes_need_the_page_token_and_a_local_host(self) -> None:
        self.task("seen")
        self.sync()
        server = dashboard.serve(self.cfg, "127.0.0.1", 0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=10) as response:
            state = json.load(response)
        self.assertEqual((state["counts"], state["tasks"][0]["task_id"]), ({"READY": 1}, "seen"))
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as response:
            self.assertIn(server.workboard_token, response.read().decode())
        act = {"op": "cancel", "args": {"task": "seen"}}
        self.assertEqual(self.post(port, act, {})[0], 403)
        self.assertEqual(self.post(port, act, {"X-Workboard-Token": "wrong"})[0], 403)
        self.assertEqual(self.post(port, act, {"X-Workboard-Token": server.workboard_token, "Host": "evil.example"})[0], 403)
        self.assertEqual(self.status("seen"), "READY")
        self.assertFalse(self.post(port, {"op": "rm -rf /", "args": {}}, {"X-Workboard-Token": server.workboard_token})[1]["ok"])
        code, body = self.post(port, act, {"X-Workboard-Token": server.workboard_token})
        self.assertEqual((code, body["ok"], self.status("seen")), (200, True, "CANCELLED"))
        actor = self.conn.execute("SELECT actor FROM events WHERE kind='cancelled'").fetchone()[0]
        self.assertEqual(actor, "human")


class Workers(ProjectCase):
    def wait(self, check, seconds: float = 20.0) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            if check():
                return True
            time.sleep(0.2)
        return False

    def test_started_workers_finish_the_board_and_a_resize_drains_instead_of_killing(self) -> None:
        self.task("bg")
        self.sync()
        os.environ["WORKBOARD_ROOT"] = str(self.repo)
        self.addCleanup(os.environ.pop, "WORKBOARD_ROOT", None)
        started = control.set_slots(self.cfg, self.conn, "alpha", 1)
        control.set_slots(self.cfg, self.conn, "beta", 1)
        self.addCleanup(control.stop_now, self.cfg, self.conn)
        self.assertEqual(started["started"], [1])
        self.assertTrue(self.wait(lambda: self.status("bg") == "DONE"), self.row("bg")["reason"])
        self.assertIn("bg.txt", self.remote_files())
        self.assertTrue(self.wait(lambda: self.conn.execute("SELECT COUNT(*) FROM workers").fetchone()[0] == 0))


if __name__ == "__main__":
    unittest.main()
