#!/usr/bin/env python3
"""A stand-in agent CLI for tests. It reads the prompt on stdin and acts on `FAKE:` markers in the brief."""
import json
import re
import subprocess
import sys
from pathlib import Path

text = sys.stdin.read()
head = re.search(r"<!-- workboard role=(\S+) task=(\S+) handoff=(\S+) verdict=(\S+) -->", text)
role, task, handoff, verdict = head.group(1), head.group(2), Path(head.group(3)), Path(head.group(4))


def git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


if role == "author":
    if "FAKE:quota" in text:
        print("error: usage limit reached")
        sys.exit(1)
    if "FAKE:nothing" in text:
        sys.exit(0)
    name = re.search(r"FAKE:file=(\S+)", text)
    target = Path(name.group(1) if name else f"{task}.txt")
    seen = handoff / "author-runs"
    runs = int(seen.read_text()) + 1 if seen.exists() else 1
    seen.write_text(str(runs))
    target.write_text(f"{task} attempt {runs}\n")
    if "FAKE:dirty" in text and runs == 1:
        sys.exit(0)
    git("add", "--", str(target))
    git("commit", "-m", f"{task}: attempt {runs}")
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
