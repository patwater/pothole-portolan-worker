#!/usr/bin/env python3
"""Render the GitHub issue text for a funnel run.

The workflow keeps one open issue labelled funnel-summary. Its body is a standing "waiting on
the supervisor" list built from the repository, and each run adds one comment saying what ran and
what it cost. This script only writes two Markdown files; the workflow posts them with gh.

Usage: python3 -I scripts/funnel_report.py --root DIR --summary run.json --body body.md --comment comment.md
"""
import argparse
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import next_behavior  # noqa: E402

TITLE = "Funnel summary: what is waiting on the supervisor"
LABEL = "funnel-summary"
OUTCOME_TEXT = {
    "paused": "Paused: a PAUSE file exists, so nothing ran.",
    "wrong_phase": "Did not run: permissions.yaml is not at phase 2.",
    "cap_unset": "Did not run: `spend.daily_cap_usd` in permissions.yaml is still null.",
    "dirty": "Did not run: the working tree was not clean.",
    "cap_reached": "Stopped: the daily spend cap was reached.",
    "nothing_to_do": "Stopped: nothing left to do.",
    "draft_queued": "Stopped: a draft is waiting for the supervisor.",
    "blocked": "Stopped: a city is blocked.",
    "fetch_incomplete": "Stopped: Fetch kept what it fetched and left some sources to retry on the next run.",
    "max_behaviors": "Stopped: the per-run limit of behaviors was reached.",
    "failed": "Stopped: a behavior failed and was discarded.",
}


def front_matter(path):
    m = re.match(r"\A---\n(.*?)\n---\n", path.read_text(encoding="utf-8"), re.S)
    try:
        data = yaml.safe_load(m.group(1)) if m else None
    except yaml.YAMLError:
        data = None
    return data if isinstance(data, dict) else {}


def waiting(root):
    root = Path(root)
    drafts = sorted(p.name for p in (root / "queue" / "drafts").glob("*") if p.name != ".gitkeep")
    resolved = next_behavior.resolved_proposals(root)
    proposals = []
    for p in sorted((root / "queue" / "proposals").glob("*.md")):
        pid = str(front_matter(p).get("id", p.stem))
        if pid not in resolved:
            proposals.append(p.name)
    blocked = []
    for slug in sorted(q.name for q in (root / "cities").iterdir() if q.is_dir()) if (root / "cities").is_dir() else []:
        try:
            status = next_behavior.load_status(root, slug)
        except next_behavior.StatusError:
            blocked.append(f"{slug}: status.yaml is missing or invalid")
            continue
        block = status and status["blocked_on"]
        if block and not (block["type"] == "proposal" and block["ref"] in resolved):
            blocked.append(f"{slug}: {block['type']} {block['ref']}. {block['note']}")
    return {"drafts": drafts, "proposals": proposals, "blocked": blocked}


def bullets(items, empty):
    return "\n".join(f"- {i}" for i in items) if items else f"- {empty}"


def render_body(root, summary):
    w = waiting(root)
    cap = summary.get("daily_cap_usd")
    cap_line = ("`daily_cap_usd` is **null**. No scheduled run spends anything until you set it in permissions.yaml."
                if cap is None else f"Daily cap ${cap:.2f}; spent so far today ${summary.get('spend_today_usd', 0):.2f}.")
    return (f"This issue is rewritten by the funnel workflow after every run. Each run adds a comment below.\n\n"
            f"## Drafts waiting for you (queue/drafts/)\n\n{bullets(w['drafts'], 'none')}\n\n"
            f"## Proposals with no decision yet (queue/proposals/)\n\n{bullets(w['proposals'], 'none')}\n\n"
            f"## Cities that will not run until you act\n\n{bullets(w['blocked'], 'none')}\n\n"
            f"## Spending\n\n{cap_line}\n")


def render_comment(summary):
    outcome = summary["outcome"]
    lines = [f"**{summary.get('date', '')}, run {summary.get('run_id', '')}.** "
             f"{OUTCOME_TEXT.get(outcome, outcome)}", "", summary.get("message", "")]
    if summary.get("run_url"):
        lines += ["", f"Workflow run: {summary['run_url']}"]
    steps = summary.get("steps") or []
    if steps:
        lines += ["", "| # | Behavior | City | Result | Stage | Turns | Cost |", "|---|---|---|---|---|---|---|"]
        for s in steps:
            cost = "unknown" if s.get("cost_usd") is None else f"${s['cost_usd']:.4f}"
            lines.append(f"| {s['n']} | {s['behavior']} | {s['city']} | {s['outcome']} | "
                         f"{s['stage_before']} to {s['stage_after'] or s['stage_before']} | {s.get('turns')} | {cost} |")
        for s in steps:
            if s.get("outcome") == "failed" and s.get("detail"):
                lines += ["", f"Step {s['n']} failed, and its changes were discarded:", "", "```",
                          s["detail"][:1200], "```"]
    lines += ["", f"Cost of this run ${summary.get('run_cost_usd', 0):.4f}. Spent today ${summary.get('spend_today_usd', 0):.4f}."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".")
    ap.add_argument("--summary", required=True)
    ap.add_argument("--body", required=True)
    ap.add_argument("--comment", required=True)
    args = ap.parse_args(argv)
    summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
    Path(args.body).write_text(render_body(args.root, summary), encoding="utf-8")
    Path(args.comment).write_text(render_comment(summary), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
