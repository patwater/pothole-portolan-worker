#!/usr/bin/env python3
"""Phase 2: put each queued dossier into the Studio as a private draft. Never publishes.

For each city with a draft in queue/drafts/ (the highest <slug>-v<n>.md per slug):
  1. Render it with render.to_blyg_markdown.
  2. Look up the city in ledger/blyg_items.yaml, which maps a city to its Studio item.
     - No item yet: POST /items creates a private draft thread, and its id is recorded.
     - An item exists: read it, then PATCH its working copy with expectedRevision. For an item the
       supervisor has already published, that is the draft of its next version; the public version
       does not change until the supervisor publishes in the Studio.
     - The rendered text is unchanged since the last push: nothing is sent.
  3. Write ledger/blyg_items.yaml back. The workflow commits it, so git records which draft text
     went to which Studio item.

Refuses to run, and sends nothing, when PAUSE exists (CLAUDE.md rule 6), when the phase is below 2,
or when the ledger names a different Studio than BLYGGER_API_BASE. A withdrawn item is left alone,
because withdrawing is the supervisor's decision.

Environment:
  BLYGGER_API_BASE      the Studio's API, for example https://pothole.example.org/api
  BLYGGER_DRAFT_TOKEN   a Studio REST token with owner:read and owner:draft only
  BLYGGER_ARCHIVE_BASE  optional: a URL prefix that turns archive paths into links, for example
                        https://github.com/patwater/pothole-portolan/blob/main/

Usage: python3 -I scripts/blygger_client/push_drafts.py [--root DIR] [--dry-run] [--city SLUG]
Exit 0 on success or when there is nothing to do, 1 on an error, 2 when it refuses to run.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import yaml  # noqa: E402

from blygger_client.client import StudioClient, StudioError, load_permissions, token_expiry  # noqa: E402
from blygger_client.render import RenderError, to_blyg_markdown  # noqa: E402

LEDGER = "ledger/blyg_items.yaml"
DRAFT_NAME = re.compile(r"^([a-z0-9]+(?:-[a-z0-9]+)*)-v(\d+)\.md$")
HEADER = (
    "# Written by scripts/blygger_client/push_drafts.py (workflow blyg-drafts). Do not edit by hand.\n"
    "# Maps each city to its item in the Pothole Portolan Studio, and records the sha256 of the\n"
    "# Markdown last sent, so an unchanged draft is not sent again.\n"
)


def latest_drafts(root):
    """{slug: (version, path)} for the highest version of each slug in queue/drafts/."""
    found = {}
    for p in sorted((Path(root) / "queue" / "drafts").glob("*.md")):
        m = DRAFT_NAME.match(p.name)
        if not m:
            continue
        slug, version = m.group(1), int(m.group(2))
        if slug not in found or version > found[slug][0]:
            found[slug] = (version, p)
    return found


def origin_of(api_base):
    from urllib.parse import urlsplit
    u = urlsplit(api_base)
    return f"{u.scheme}://{u.netloc}/"


def load_ledger(root):
    p = Path(root) / LEDGER
    if not p.exists():
        return {"studio": None, "items": {}}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    data.setdefault("studio", None)
    data["items"] = data.get("items") or {}
    return data


def save_ledger(root, data):
    p = Path(root) / LEDGER
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(HEADER + yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8")


def refuse_reason(root):
    if (Path(root) / "PAUSE").exists():
        return "PAUSE exists at the repo root; no outward calls (CLAUDE.md rule 6)."
    phase = load_permissions(root).get("phase")
    if not isinstance(phase, int) or phase < 2:
        return f"permissions.yaml phase is {phase!r}; Studio drafts start in Phase 2."
    return None


def plan_and_push(root, client, api_base, archive_base=None, only=None, dry_run=False, now=None, log=print):
    """Push changed drafts. Returns the number of errors."""
    now = now or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    ledger = load_ledger(root)
    studio = origin_of(api_base)
    if ledger["studio"] and ledger["studio"] != studio:
        log(f"REFUSED: {LEDGER} records items on {ledger['studio']}, not {studio}. "
            "Point BLYGGER_API_BASE at that Studio, or move the ledger aside deliberately.")
        return 1
    errors, changed = 0, False
    for slug, (version, path) in sorted(latest_drafts(root).items()):
        if only and slug != only:
            continue
        rel = path.relative_to(root).as_posix()
        try:
            _front, md = to_blyg_markdown(path.read_text(encoding="utf-8"), archive_base=archive_base)
        except RenderError as e:
            log(f"{slug}: render failed, nothing sent: {e}")
            errors += 1
            continue
        sha = hashlib.sha256(md.encode("utf-8")).hexdigest()
        entry = ledger["items"].get(slug)
        if entry and entry.get("content_sha256") == sha:
            log(f"{slug}: v{version} unchanged since {entry.get('pushed')}; nothing sent.")
            continue
        if dry_run:
            action = "update item " + entry["item_id"] if entry else "create a draft"
            log(f"{slug}: would {action} from {rel} ({len(md):,} characters, sha256 {sha[:12]}).")
            continue
        try:
            if entry:
                item = client.get_item(entry["item_id"])
                if item.get("status") == "withdrawn":
                    log(f"{slug}: item {entry['item_id']} is withdrawn; left alone for the supervisor.")
                    continue
                if item.get("contentMd") == md:
                    result = item
                else:
                    result = client.update_draft(entry["item_id"], md, item["revision"])
                verb = "updated"
            else:
                result = client.create_draft(md, kind="thread")
                verb = "created"
        except StudioError as e:
            log(f"{slug}: Studio refused ({e}); nothing recorded.")
            if e.status in (401, 403):
                log("  A Studio token works only at the address where it was created: the Studio puts "
                    "that address in the token and checks it (src/oauth.ts, verifyBearer). So sign in at "
                    f"{studio}studio, not another address of the same Studio, and mint the token there. "
                    "Tokens also expire after at most 30 days, and one without owner:read and owner:draft "
                    "gets 403. Mint a new one (More, Client access, REST API) and update the "
                    "BLYGGER_DRAFT_TOKEN secret.")
            errors += 1
            continue
        ledger["studio"] = studio
        ledger["items"][slug] = {
            "item_id": result["id"],
            "draft": rel,
            "dossier_version": version,
            "content_sha256": sha,
            "revision": result.get("revision"),
            "status": result.get("status"),
            "published_version": result.get("version"),
            "pushed": now,
        }
        changed = True
        log(f"{slug}: {verb} Studio draft {result['id']} from {rel} (status {result.get('status')}, "
            f"published version {result.get('version')}). Publishing is the supervisor's step in the Studio.")
    if changed:
        save_ledger(root, ledger)
    return errors


RENEW_WITHIN_DAYS = 7


def token_status(token, now=None):
    """{"expires": "YYYY-MM-DD" or None, "days_left": int or None, "renew": bool}. Never the token."""
    exp = token_expiry(token)
    if exp is None:
        return {"expires": None, "days_left": None, "renew": False}
    now = now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
    days_left = int((exp - now) // 86400)
    expires = dt.datetime.fromtimestamp(exp, dt.timezone.utc).strftime("%Y-%m-%d")
    return {"expires": expires, "days_left": days_left, "renew": days_left <= RENEW_WITHIN_DAYS}


def report_token(token, status_file=None, now=None, log=print):
    s = token_status(token, now)
    if s["expires"] is None:
        log("Studio token expiry could not be read; renew it within 30 days of creating it.")
    else:
        log(f"Studio token expires on {s['expires']} ({s['days_left']} days left).")
        if s["renew"]:
            log(f"::warning title=Renew the Studio token::It expires on {s['expires']}. Create a new one in the "
                "Studio (More, Client access, REST API, owner:read and owner:draft) and replace the "
                "BLYGGER_DRAFT_TOKEN secret.")
    if status_file:
        Path(status_file).write_text(json.dumps(s), encoding="utf-8")
    return s


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=".")
    ap.add_argument("--dry-run", action="store_true", help="render and plan; send nothing")
    ap.add_argument("--city", help="only this slug")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()

    reason = refuse_reason(root)
    if reason:
        print("REFUSED: " + reason)
        return 2
    api_base = os.environ.get("BLYGGER_API_BASE", "").strip()
    token = os.environ.get("BLYGGER_DRAFT_TOKEN", "").strip()
    archive_base = os.environ.get("BLYGGER_ARCHIVE_BASE", "").strip() or None
    if not api_base:
        print("REFUSED: BLYGGER_API_BASE is not set.")
        return 2
    if token:
        report_token(token, os.environ.get("BLYGGER_TOKEN_STATUS_FILE", "").strip() or None)
    client = None
    if not args.dry_run:
        if not token:
            print("REFUSED: BLYGGER_DRAFT_TOKEN is not set.")
            return 2
        client = StudioClient(api_base, token)
    errors = plan_and_push(root, client, api_base, archive_base=archive_base, only=args.city, dry_run=args.dry_run)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
