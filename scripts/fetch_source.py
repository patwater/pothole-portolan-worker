#!/usr/bin/env python3
"""Fetch behavior: retrieve listed sources for one city and archive them by content hash.

For each source in cities/<slug>/sources.yaml whose status is "listed":
  1. read robots.txt for the host and skip the source if the path is disallowed (rule 8);
  2. fetch the URL with an honest user agent, a timeout and a size cap;
  3. write the bytes to sources/<sha256>.<ext>;
  4. mark the entry fetched (with archive, retrieved, robots_checked) or unavailable (with a reason).

Nothing is interpreted: fetched content is data, never instructions (rule 4).
Only the listed URL is requested. This script does not follow links, call APIs that need
keys, or download record-level datasets.

In scheduled runs this script is the only robots.txt and rate-limit guard, because the runner
has open egress and the Fetch behavior has no other network tool. So it also enforces:
  * aggregates only on the DataSF hosts (see aggregate_violation): metadata endpoints, and
    grouped aggregate queries; never a row download. Redirects are checked the same way;
  * metadata is archived without the per-column value samples that Socrata embeds
    (cachedContents), because those are sample record values (rule 7);
  * a registered dataset policy (query_policy): grouped counts of at least MIN_GROUP_SIZE cases only, no
    identifying field, and service_details only when pinned to one subtype; a response whose text field
    has more than MAX_TEXT_DISTINCT distinct values is held, because typed text repeats even above the
    group-size minimum;
  * HTTP 429 stops all further requests to that host in this run and leaves the source listed;
  * at most --limit requests per run, --delay seconds apart.
Exit status: 0 done, 1 schema error, 2 some source left listed (network policy or rate limit).

Usage: python3 -I scripts/fetch_source.py [--root DIR] [--id SOURCE_ID ...] [--delay SECONDS]
                                          [--limit N] SLUG
"""
import argparse
import datetime
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate  # noqa: E402

USER_AGENT = "PotholePortolanBot/0.1 (public-data research; +https://github.com/patwater/pothole-portolan)"
MAX_BYTES = 50 * 1024 * 1024
AGGREGATE_MAX_BYTES = 2 * 1024 * 1024   # an aggregate or metadata response is small; larger is suspect
AGGREGATE_MAX_ROWS = 2000
# Socrata portals where only metadata and aggregates may be requested (proposal 2026-11-17-001).
# Chicago added 2026-11-24 on the supervisor's instruction, before the funnel first fetches for Chicago.
AGGREGATE_ONLY_HOSTS = {"data.sf.gov", "data.sfgov.org", "data.cityofchicago.org"}
DATASET_ID = r"[a-z0-9]{4}-[a-z0-9]{4}"
METADATA_PATH = re.compile(rf"^/api/views/{DATASET_ID}(?:\.json|/columns\.json|/columns/\d+\.json)?$")
RESOURCE_PATH = re.compile(rf"^/resource/{DATASET_ID}\.(?:json|csv)$")
AGGREGATE_FUNCTIONS = ("count", "sum", "avg", "min", "max", "median", "stddev_pop", "stddev_samp")
# Group keys that would turn an aggregate into a record listing or expose a person or place.
FORBIDDEN_GROUP_KEY = re.compile(
    r"(address|addr\b|street|lat\b|latitude|long\b|lon\b|longitude|location|point|geom|coordinate|"
    r"(first|last|full|requester|reporter|customer|resident|contact|owner)_?name|phone|email|"
    r"description|comment|notes?|media|url|requester|reporter|caller|"
    r"(^|_)id(\b|_)|request_id|case_id|ticket|number)", re.I)
TIMEOUT = 60
# A grouped query over millions of 311 rows can take minutes to answer. A flat 60 seconds timed
# out on Chicago's current feed (run 45340105700), and it would time out again on every retry.
QUERY_TIMEOUT = 240
EXT_BY_TYPE = {
    "text/html": ".html", "application/xhtml+xml": ".html", "application/pdf": ".pdf",
    "text/csv": ".csv", "application/json": ".json", "text/plain": ".txt",
    "application/xml": ".xml", "text/xml": ".xml",
}
HEADER = (
    "# Maintained by scripts/fetch_source.py (Fetch) from Discover's list. Status values:\n"
    "# listed (not yet fetched), fetched (archived under sources/), unavailable (reason in notes).\n"
)

_robots = {}


def utc_today():
    """Retrieval dates are UTC dates, the same rule checks/run_checks.py applies."""
    return datetime.datetime.now(datetime.timezone.utc).date()


def _split_top_level(text):
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _strip_alias(expr):
    return re.sub(r"\s+as\s+\w+\s*$", "", expr.strip(), flags=re.I).strip()


def aggregate_violation(url):
    """Return None when url is allowed on an aggregate-only host, else the reason it is not.

    Allowed: dataset metadata and column metadata, and /resource/<id>.json|csv queries whose
    $select holds only aggregate calls and the $group keys, with a $group and a $limit of at
    most AGGREGATE_MAX_ROWS. Hosts not in AGGREGATE_ONLY_HOSTS are not restricted here.
    """
    parts = urllib.parse.urlsplit(url)
    if (parts.hostname or "").lower() not in AGGREGATE_ONLY_HOSTS:
        return None
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    if METADATA_PATH.match(parts.path):
        return None if not query else "metadata endpoints take no query"
    if not RESOURCE_PATH.match(parts.path):
        return f"path {parts.path} is not a metadata or aggregate endpoint (row downloads are refused)"
    unknown = sorted(k for k in query if k not in ("$select", "$group", "$having", "$where", "$order", "$limit"))
    if unknown:
        return f"query parameter(s) {unknown} are not allowed"
    dataset = re.match(rf"^/resource/({DATASET_ID})\.", parts.path).group(1)
    rule = DATASET_POLICY.get(dataset)
    # A registered dataset of department performance figures (not records about people) may be read as rows.
    # Everything else on these hosts must be a grouped aggregate.
    if not (rule is not None and not rule["aggregate_only"]):
        group = query.get("$group", [""])[0].strip()
        select = query.get("$select", [""])[0].strip()
        if not group or not select:
            return "an aggregate query needs both $select and $group"
        group_keys = [_strip_alias(g) for g in _split_top_level(group)]
        for key in group_keys:
            if FORBIDDEN_GROUP_KEY.search(key):
                return f"$group key {key!r} could identify a person, place or record"
        for item in _split_top_level(select):
            expr = _strip_alias(item)
            alias = re.search(r"\s+as\s+(\w+)\s*$", item.strip(), re.I)
            is_aggregate = re.match(rf"^({'|'.join(AGGREGATE_FUNCTIONS)})\(", expr, re.I)
            grouped = expr in group_keys or (alias and alias.group(1) in group_keys)
            if not is_aggregate and not grouped:
                return f"$select item {expr!r} is neither an aggregate nor a $group key"
    limit = query.get("$limit", [""])[0]
    # A registered dataset is also held to query_policy (minimum group size, typed-text guard), so it may use that
    # policy's $limit. What gets archived is still capped at AGGREGATE_MAX_ROWS rows (aggregate_rows_exceeded).
    cap = rule["max_limit"] if rule is not None else AGGREGATE_MAX_ROWS
    if not limit.isdigit() or not 0 < int(limit) <= cap:
        return f"$limit must be a number from 1 to {cap}"
    return None


def _drop_value_samples(node):
    if isinstance(node, dict):
        return {k: _drop_value_samples(v) for k, v in node.items() if k != "cachedContents"}
    if isinstance(node, list):
        return [_drop_value_samples(v) for v in node]
    return node


def reduce_body(url, body):
    """Return (bytes_to_archive, note). Socrata metadata loses its per-column value samples."""
    parts = urllib.parse.urlsplit(url)
    if (parts.hostname or "").lower() in AGGREGATE_ONLY_HOSTS and METADATA_PATH.match(parts.path):
        reduced = _drop_value_samples(json.loads(body.decode("utf-8")))
        text = json.dumps(reduced, indent=1, ensure_ascii=False) + "\n"
        return text.encode("utf-8"), ("Archived copy is reduced: the per-column value samples "
                                       "(cachedContents: top values, smallest, largest) were removed "
                                       "because they embed sample record values (rule 7).")
    return body, ""


def aggregate_rows_exceeded(url, body):
    parts = urllib.parse.urlsplit(url)
    if (parts.hostname or "").lower() not in AGGREGATE_ONLY_HOSTS or not RESOURCE_PATH.match(parts.path):
        return False
    if parts.path.endswith(".json"):
        data = json.loads(body.decode("utf-8"))
        return isinstance(data, list) and len(data) > AGGREGATE_MAX_ROWS
    return body.count(b"\n") > AGGREGATE_MAX_ROWS + 1
# Rule 7 for portal queries. The 311 dataset is record-level, so only grouped counts may be requested,
# and no query may mention a field that can identify a person or a private address. A dataset id that is
# not listed here is refused (fail closed). The Scorecard Measures dataset holds department performance
# figures, not records about people, so ordinary filtered reads are allowed for it.
SENSITIVE_FIELDS = {
    "address", "street", "lat", "long", "point", "point_address", "point_city", "point_geom", "point_state",
    "point_zip", "status_notes", "media_url", "service_request_id",
    # Chicago 311 columns that locate or identify a single request (added 2026-11-24, ahead of the first
    # Chicago fetch; names to be confirmed against the dataset's column metadata, which may be fetched).
    "street_address", "street_number", "street_name", "street_direction", "street_type", "latitude",
    "longitude", "x_coordinate", "y_coordinate", "location", "sr_number", "parent_sr_number",
    "legacy_sr_number",
}
# Fields that may appear only inside a narrowly scoped query. service_details is the third level of the 311
# case taxonomy: it is needed to tell potholes from other pavement defects, but it can hold typed text, so it
# is only allowed when the query is pinned to one subtype. Minimum group size and the response-side
# typed-text guard still apply.
SCOPED_FIELDS = {
    "service_details": r"lower\(service_subtype\)\s*=\s*'pavement_defect'",
}
MIN_GROUP_SIZE = 10
DATASET_POLICY = {
    "vw6y-z8j6": {"aggregate_only": True, "max_limit": 5000},   # SF 311 Cases (record-level)
    "kc49-udxn": {"aggregate_only": False, "max_limit": 5000},  # SF Scorecard Measures (department measures)
    # Chicago, registered 2026-11-24 before the first Chicago fetch. Both are record-level 311 data, so only
    # grouped counts are allowed. If Discover finds that the ids differ, the real ones are refused until
    # registered, which is the safe way to be wrong.
    "v6vf-nfxy": {"aggregate_only": True, "max_limit": 5000},   # Chicago 311 Service Requests (record-level)
    "7as2-ds3y": {"aggregate_only": True, "max_limit": 5000},   # Chicago 311 Pot Holes Reported, historical (record-level)
}


def query_policy(url):
    """Return None if the URL may be fetched, or a refusal reason. Applies to /resource/ query URLs only."""
    parts = urllib.parse.urlsplit(url)
    m = re.match(r"^/resource/([a-z0-9]{4}-[a-z0-9]{4})\.(json|csv)$", parts.path)
    if not m:
        return "a /resource/ URL must name a dataset id and end in .json or .csv" if parts.path.startswith("/resource/") else None
    rule = DATASET_POLICY.get(m.group(1))
    if rule is None:
        return f"dataset {m.group(1)} has no query policy, so it is refused"
    params = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    soql = {k: " ".join(v) for k, v in params.items() if k.startswith("$")}
    unknown = [k for k in params if not k.startswith("$")]
    if unknown:
        return f"plain filter parameters are not allowed ({', '.join(unknown)}); use $where"
    # Quoted literals are values, not columns: 'Street Defects' does not select the street column.
    no_literals = re.sub(r"'(?:[^']|'')*'", "''", " ".join(soql.values()))
    mentioned = set(re.findall(r"[a-z_][a-z0-9_]*", no_literals.lower()))
    banned = sorted(mentioned & SENSITIVE_FIELDS)
    if banned:
        return "query mentions sensitive field(s): " + ", ".join(banned)
    for field, scope in SCOPED_FIELDS.items():
        if field in mentioned and not re.search(scope, soql.get("$where", "").lower()):
            return f"{field} may only be queried when $where pins the subtype (matching {scope})"
    if rule["aggregate_only"]:
        if "$group" not in soql or "count(" not in soql.get("$select", "").lower():
            return "aggregate-only dataset: the query needs $group and a count() in $select"
        if "$query" in soql:
            return "$query is not allowed on an aggregate-only dataset"
        # Small groups can be single records: category fields in the 311 data hold typed text, so a one-case
        # group may be a business name and address. Require a minimum group size, applied by the portal.
        m2 = re.search(r"count\(\s*\*\s*\)\s*>=\s*(\d+)", soql.get("$having", ""))
        if m2 is None or int(m2.group(1)) < MIN_GROUP_SIZE:
            return f"aggregate-only dataset: $having count(*) >= {MIN_GROUP_SIZE} (or more) is required so small groups are never returned"
    limit = soql.get("$limit")
    if limit is None or not limit.isdigit() or int(limit) > rule["max_limit"]:
        return f"$limit must be given and at most {rule['max_limit']}"
    return None


MAX_TEXT_DISTINCT = 100


def typed_text_fields(body):
    """Return {field: distinct_count} for text fields of an aggregate response with more than MAX_TEXT_DISTINCT values.

    A grouped count is only safe to archive while the grouped fields are controlled vocabularies. A text
    field with hundreds of distinct values is the signature of typed text (names, addresses), and a minimum
    group size does not remove it: an applicant who files ten permits still appears by name.
    """
    rows = json.loads(body.decode("utf-8"))
    if not isinstance(rows, list):
        return {}
    values = {}
    for row in rows:
        for k, v in row.items():
            if isinstance(v, str):
                values.setdefault(k, set()).add(v)
    def not_text(x):
        # Numbers and ISO dates or timestamps are values of a structured field, not typed text.
        return x.lstrip("-").replace(".", "", 1).isdigit() or re.match(r"^\d{4}-\d{2}-\d{2}([T ][\d:.+\-Z]*)?$", x) is not None

    return {k: len(v) for k, v in values.items() if len(v) > MAX_TEXT_DISTINCT and not all(not_text(x) for x in v)}


def typed_text_in_response(url, body):
    """Typed-text guard for responses from a registered aggregate-only dataset on a restricted host."""
    parts = urllib.parse.urlsplit(url)
    m = re.match(r"^/resource/([a-z0-9]{4}-[a-z0-9]{4})\.json$", parts.path)
    rule = DATASET_POLICY.get(m.group(1)) if m else None
    if (parts.hostname or "").lower() not in AGGREGATE_ONLY_HOSTS or not rule or not rule["aggregate_only"]:
        return {}
    return typed_text_fields(body)


def sanitize(url, content_type, body):
    """Remove sample values that open data portals embed in dataset metadata (rule 7).

    Socrata /api/views/ JSON carries `cachedContents`: up to 20 sample values per column drawn from the
    live data, which for a 311 dataset include addresses, coordinates and free-text notes. Those are
    record-level fragments, so they are dropped before anything is archived. Returns (body, note or None).
    """
    if "/api/views/" not in urllib.parse.urlsplit(url).path or "json" not in (content_type or "").lower():
        return body, None
    removed = 0

    def strip(node):
        nonlocal removed
        if isinstance(node, dict):
            if "cachedContents" in node:
                removed += 1
                node = {k: v for k, v in node.items() if k != "cachedContents"}
            return {k: strip(v) for k, v in node.items()}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    parsed = json.loads(body.decode("utf-8"))
    cleaned = strip(parsed)
    if removed == 0:
        return body, None  # nothing left to remove, for example after reduce_body ran
    out = json.dumps(cleaned, sort_keys=True, indent=1, ensure_ascii=False).encode("utf-8")
    note = (f"Archived copy has {removed} `cachedContents` blocks (sample column values, including addresses and free text) removed "
            "and the JSON re-serialized with sorted keys, under rule 7; its sha256 is of this sanitized file, not of the raw response.")
    return out, note



# A proxy refusal is the sandbox's network policy, not a fact about the source. Such sources
# stay "listed" and are reported, never marked unavailable.
BLOCKED = "blocked by the environment's network policy"
# A network error, timeout or HTTP 5xx says nothing about the source either. Those sources also
# stay "listed" and are retried on a later run; only a refusal (robots.txt, 4xx) is "unavailable".
TRANSIENT = "temporary network failure"


def robots_allows(url):
    """Return (allowed, detail). Fails closed when robots.txt cannot be read for a reason other than 404."""
    parts = urllib.parse.urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        req = urllib.request.Request(origin + "/robots.txt", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                rp.parse(resp.read(1_000_000).decode("utf-8", errors="replace").splitlines())
            _robots[origin] = (rp, "robots.txt read")
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                rp.parse([])
                _robots[origin] = (rp, f"no robots.txt (HTTP {exc.code})")
            elif exc.code >= 500:
                _robots[origin] = (None, TRANSIENT)
            else:
                _robots[origin] = (None, f"robots.txt returned HTTP {exc.code}")
        except Exception as exc:  # network failure: do not guess
            if "Tunnel connection failed" in str(exc):
                _robots[origin] = (None, BLOCKED)
            else:
                _robots[origin] = (None, TRANSIENT)
    rp, detail = _robots[origin]
    if rp is None:
        return False, detail
    return rp.can_fetch(USER_AGENT, url), detail


def extension_for(content_type, final_url):
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in EXT_BY_TYPE:
        return EXT_BY_TYPE[ctype]
    suffix = Path(urllib.parse.urlsplit(final_url).path).suffix.lower()
    return suffix if suffix and len(suffix) <= 6 and suffix[1:].isalnum() else ".bin"


class RobotsDisallowed(Exception):  # also raised for an aggregate-only refusal
    pass


class RobotsAwareRedirects(urllib.request.HTTPRedirectHandler):
    """Check robots.txt for every redirect target before following it (rule 8)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        reason = aggregate_violation(newurl)
        if reason:
            raise RobotsDisallowed(f"redirect to {newurl}: {reason}")
        allowed, detail = robots_allows(newurl)
        if not allowed:
            raise RobotsDisallowed(f"redirect to {newurl}: " +
                                   ("disallowed by robots.txt for this user agent"
                                    if detail == "robots.txt read" else detail))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def timeout_for(url):
    """Seconds to wait for a response: longer for a grouped query on an open data portal."""
    parts = urllib.parse.urlsplit(url)
    on_portal = (parts.hostname or "").lower() in AGGREGATE_ONLY_HOSTS
    return QUERY_TIMEOUT if on_portal and RESOURCE_PATH.match(parts.path) else TIMEOUT


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    opener = urllib.request.build_opener(RobotsAwareRedirects)
    parts = urllib.parse.urlsplit(url)
    on_portal = (parts.hostname or "").lower() in AGGREGATE_ONLY_HOSTS
    cap = AGGREGATE_MAX_BYTES if on_portal else MAX_BYTES
    with opener.open(req, timeout=timeout_for(url)) as resp:
        body = resp.read(cap + 1)
        if len(body) > cap:
            raise ValueError(f"larger than {cap} bytes")
        return body, resp.geturl(), resp.headers.get("Content-Type", ""), resp.status


MAX_TRANSIENT_ATTEMPTS = 3
RETRY_MARK = "left listed to retry later"


def leave_listed(entry, today, why):
    """Record a temporary failure. Returns True if the source stays listed for another run.

    A temporary failure says nothing about the source, so the source is retried, but not forever:
    on the third attempt it is recorded as unavailable, with the reason in its notes.
    """
    attempts = (entry.get("notes") or "").count(RETRY_MARK) + 1
    if attempts >= MAX_TRANSIENT_ATTEMPTS:
        entry["status"] = "unavailable"
        entry["retrieved"] = today
        append_note(entry, f"Fetch {today}: {why}. Attempt {attempts} of {MAX_TRANSIENT_ATTEMPTS}, so recorded as "
                           "unavailable because of repeated temporary failures, not because the source refused.")
        return False
    append_note(entry, f"Fetch {today}: {why}; {RETRY_MARK} (attempt {attempts} of {MAX_TRANSIENT_ATTEMPTS}).")
    return True


def advance_stage(root, slug):
    """Fetch has no file-writing tool, so this script moves the city from discovered to fetched."""
    path = Path(root) / "cities" / slug / "status.yaml"
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    if re.search(r"^stage: discovered\s*$", text, re.M):
        path.write_text(re.sub(r"^stage: discovered\s*$", "stage: fetched", text, count=1, flags=re.M),
                        encoding="utf-8")
        print("STAGE       discovered -> fetched (no listed source remains)")


def append_note(entry, text):
    entry["notes"] = (entry.get("notes", "").rstrip() + " " if entry.get("notes") else "") + text


def run(root, slug, only_ids, delay, limit=20):
    path = Path(root) / "cities" / slug / "sources.yaml"
    sources = validate.load_yaml(path)
    today = utc_today().isoformat()
    archive_dir = Path(root) / "sources"
    archive_dir.mkdir(exist_ok=True)
    first = True
    blocked = 0
    requests = 0
    rate_limited_hosts = set()
    for entry in sources:
        if entry.get("status") != "listed" or (only_ids and entry["id"] not in only_ids):
            continue
        if not first:
            time.sleep(delay)
        first = False
        url = entry["url"]
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if host in rate_limited_hosts:
            print(f"RATE-LIMITED {entry['id']}: {host} sent HTTP 429 earlier in this run; left as listed")
            blocked += 1
            continue
        if requests >= limit:
            print(f"LIMIT       {entry['id']}: {limit} requests already made this run; left as listed")
            blocked += 1
            continue
        reason = aggregate_violation(url) or (query_policy(url) if host in AGGREGATE_ONLY_HOSTS else None)
        if reason:
            entry["status"] = "unavailable"
            entry["retrieved"] = today
            append_note(entry, f"Fetch {today}: not retrieved, refused by the aggregate-only rule: {reason}.")
            print(f"UNAVAILABLE {entry['id']}: aggregate-only rule: {reason}")
            continue
        requests += 1
        allowed, robots_detail = robots_allows(url)
        if robots_detail == BLOCKED:
            print(f"BLOCKED     {entry['id']}: {robots_detail}; left as listed")
            blocked += 1
            continue
        if robots_detail == TRANSIENT:
            if leave_listed(entry, today, "robots.txt could not be read (temporary network failure)"):
                print(f"BLOCKED     {entry['id']}: {robots_detail}; left as listed")
                blocked += 1
            else:
                print(f"UNAVAILABLE {entry['id']}: {robots_detail} on repeated attempts")
            continue
        if not allowed:
            entry["status"] = "unavailable"
            entry["retrieved"] = today
            reason = ("disallowed by robots.txt for this user agent" if robots_detail == "robots.txt read"
                      else robots_detail)
            append_note(entry, f"Fetch {today}: not retrieved, {reason}.")
            print(f"UNAVAILABLE {entry['id']}: {robots_detail}")
            continue
        try:
            body, final_url, ctype, status = fetch(url)
        except RobotsDisallowed as exc:
            entry["status"] = "unavailable"
            entry["retrieved"] = today
            append_note(entry, f"Fetch {today}: not retrieved, {exc}.")
            print(f"UNAVAILABLE {entry['id']}: {exc}")
            continue
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                rate_limited_hosts.add(host)
                if leave_listed(entry, today, "HTTP 429 (rate limited)"):
                    print(f"RATE-LIMITED {entry['id']}: HTTP 429 from {host}; left as listed")
                    blocked += 1
                else:
                    print(f"UNAVAILABLE {entry['id']}: HTTP 429 on repeated attempts")
                continue
            if exc.code >= 500:
                if leave_listed(entry, today, f"HTTP {exc.code}"):
                    print(f"BLOCKED     {entry['id']}: HTTP {exc.code}, {TRANSIENT}; left as listed")
                    blocked += 1
                else:
                    print(f"UNAVAILABLE {entry['id']}: HTTP {exc.code} on repeated attempts")
                continue
            entry["status"] = "unavailable"
            entry["retrieved"] = today
            append_note(entry, f"Fetch {today}: HTTP {exc.code} {exc.reason}.")
            print(f"UNAVAILABLE {entry['id']}: HTTP {exc.code}")
            continue
        except Exception as exc:
            if "Tunnel connection failed" in str(exc):
                print(f"BLOCKED     {entry['id']}: {BLOCKED}; left as listed")
                blocked += 1
                continue
            if leave_listed(entry, today, f"{type(exc).__name__}, a temporary network failure"):
                print(f"BLOCKED     {entry['id']}: {type(exc).__name__}, {TRANSIENT}; left as listed")
                blocked += 1
            else:
                print(f"UNAVAILABLE {entry['id']}: {type(exc).__name__} on repeated attempts")
            continue
        try:
            if aggregate_rows_exceeded(url, body):
                raise ValueError(f"more than {AGGREGATE_MAX_ROWS} rows is not an aggregate")
            offenders = typed_text_in_response(url, body)
            if offenders:
                raise ValueError("text field(s) with too many distinct values for a controlled vocabulary ("
                                 + ", ".join(f"{k}: {v}" for k, v in offenders.items())
                                 + "), which is the signature of typed text, so the response was held")
            body, reduced_note = reduce_body(url, body)
            body, sanitize_note = sanitize(final_url, ctype, body)
            if sanitize_note:
                reduced_note = (reduced_note + " " + sanitize_note).strip()
        except ValueError as exc:  # includes JSON decode errors
            entry["status"] = "unavailable"
            entry["retrieved"] = today
            append_note(entry, f"Fetch {today}: not archived, {exc}.")
            print(f"UNAVAILABLE {entry['id']}: not archived, {exc}")
            continue
        digest = hashlib.sha256(body).hexdigest()
        ext = extension_for(ctype, final_url)
        rel = f"sources/{digest}{ext}"
        target = Path(root) / rel
        if not target.exists():
            target.write_bytes(body)
        entry["status"] = "fetched"
        entry["retrieved"] = today
        entry["archive"] = rel
        entry["robots_checked"] = True
        detail = f"Fetch {today}: HTTP {status}, {ctype.split(';')[0] or 'unknown type'}, {len(body)} bytes"
        if final_url != url:
            detail += f", final URL {final_url}"
        append_note(entry, detail + "." + (" " + reduced_note if reduced_note else ""))
        print(f"FETCHED     {entry['id']}: {rel} ({len(body)} bytes)")
    path.write_text(HEADER + yaml.safe_dump(sources, sort_keys=False, allow_unicode=True, width=100),
                    encoding="utf-8")
    errors = validate.validate_file(path, "sources.schema.json")
    for line in errors:
        print("SCHEMA", line)
    if not errors and not any(e.get("status") == "listed" for e in sources):
        advance_stage(root, slug)
    if blocked:
        print(f"{blocked} source(s) left as listed (network policy, rate limit or request limit)")
    return 1 if errors else (2 if blocked else 0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--id", action="append", default=[])
    ap.add_argument("--delay", type=float, default=3.0, help="seconds between requests")
    ap.add_argument("--limit", type=int, default=20, help="most requests to make in one run")
    ap.add_argument("slug")
    args = ap.parse_args(argv)
    return run(args.root, args.slug, set(args.id), args.delay, args.limit)


if __name__ == "__main__":
    sys.exit(main())
