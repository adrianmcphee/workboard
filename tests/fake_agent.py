#!/usr/bin/env python3
"""A stand-in agent CLI for tests. It reads the prompt on stdin and acts on `FAKE:` markers in the brief."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

text = sys.stdin.read()
head = re.search(r"<!-- workboard role=(\S+) task=(\S+) handoff=(\S+) verdict=(\S+)((?: \w+=\S+)*) -->", text)
role, task, handoff, verdict = head.group(1), head.group(2), Path(head.group(3)), Path(head.group(4))
extra = dict(item.split("=", 1) for item in head.group(5).split())
PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)


def git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def board(*args: str) -> str:
    """Talk to Workboard the way a real agent would, through its command line."""
    done = subprocess.run([sys.executable, "-m", "workboard", "--root", extra["root"], "audit", *args],
                          capture_output=True, text=True, env={**os.environ, "PYTHONPATH": PACKAGE_PARENT})
    if done.returncode:
        sys.stderr.write(done.stderr)
        sys.exit(3)
    return done.stdout


if role == "audit":
    kind = extra.get("kind")
    if "FAKE:audit-silent" in text:
        sys.exit(0)
    if "FAKE:audit-touch" in text:
        Path("README.md").write_text("changed by an auditor\n")
        Path("stray.txt").write_text("left behind\n")
    owner = re.search(r"FAKE:audit-finding=(\S+)", text)
    if owner and kind in ("slice", "checklist"):
        board("file", "--job", task, "--task", owner.group(1), "--severity", "P1", "--title", "Export drops the header row",
              "--repro", "Export any report", "--expected", "The first row names the columns")
    if kind == "verify":
        reopen = (Path(extra["root"]) / "FAKE-verify-reopen").exists()
        for finding in sorted(set(re.findall(r"`(F-\d{4})`", text))):
            board("verify", finding, "--job", task, "--result", "reopened" if reopen else "verified", "--note", "checked")
    if kind == "triage":
        fresh = []
        for item in json.loads(board("findings", "--untriaged", "--json")):
            if item["root_cause"]:
                board("triage", "--job", task, "--group", item["finding_id"], "--root-cause", item["root_cause"])
            else:
                fresh.append(item["finding_id"])
        if fresh:
            board("triage", "--job", task, "--group", ",".join(fresh), "--title", "Shared cause")
    verdict.write_text(json.dumps({"complete": True, "summary": "audited"}))
    sys.exit(0)

if role == "author":
    if "FAKE:quota" in text:
        print("error: usage limit reached")
        sys.exit(1)
    if "FAKE:nothing" in text:
        sys.exit(0)
    name = re.search(r"FAKE:file=(\S+)", text)
    trailer = re.search(r"trailer line `([A-Za-z-]+: F-[0-9F, -]+)`", text)
    target = Path(name.group(1) if name else (f"tests/{task}.txt" if trailer else f"{task}.txt"))
    target.parent.mkdir(parents=True, exist_ok=True)
    seen = handoff / "author-runs"
    runs = int(seen.read_text()) + 1 if seen.exists() else 1
    seen.write_text(str(runs))
    target.write_text(f"{task} attempt {runs}\n")
    if "FAKE:dirty" in text and runs == 1:
        sys.exit(0)
    git("add", "--", str(target))
    git("commit", "-m", f"{task}: attempt {runs}" + (f"\n\n{trailer.group(1)}" if trailer else ""))
else:
    seen = handoff / "review-runs"
    runs = int(seen.read_text()) + 1 if seen.exists() else 1
    seen.write_text(str(runs))
    if "FAKE:silent-review" in text:
        sys.exit(0)
    if "FAKE:reject-once" in text and runs == 1:
        verdict.write_text(json.dumps({"passed": False, "defects": ["a.txt:1 wrong on first look"]}))
    else:
        verdict.write_text(json.dumps({"passed": True, "summary": "looks right"}))
