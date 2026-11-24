"""Tests for scripts/fetch_source.py. Run: python3 -I tests/test_fetch_source.py -v

No test touches the network: fetch() and robots_allows() are replaced.
"""
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import fetch_source as fs  # noqa: E402
import validate  # noqa: E402

ID = "abcd-1234"
META = f"https://data.sf.gov/api/views/{ID}.json"
AGG = (f"https://data.sf.gov/resource/{ID}.json?" + "&".join([
    "$select=" + quote("date_extract_y(requested_datetime) as year,service_name,count(*) as n"),
    "$group=" + quote("year,service_name"), "$limit=500"]))


def agg(**params):
    base = {"$select": "count(*) as n,service_name", "$group": "service_name", "$limit": "10"}
    base.update(params)
    return f"https://data.sf.gov/resource/{ID}.json?" + "&".join(f"{k}={quote(v)}" for k, v in base.items() if v is not None)


class AggregateRuleTest(unittest.TestCase):
    def test_metadata_and_a_grouped_aggregate_are_allowed_on_both_hosts(self):
        self.assertIsNone(fs.aggregate_violation(META))
        self.assertIsNone(fs.aggregate_violation(META.replace("data.sf.gov", "data.sfgov.org")))
        self.assertIsNone(fs.aggregate_violation(f"https://data.sf.gov/api/views/{ID}/columns.json"))
        self.assertIsNone(fs.aggregate_violation(AGG))

    def test_row_downloads_are_refused(self):
        for url in (f"https://data.sf.gov/api/views/{ID}/rows.csv?accessType=DOWNLOAD",
                    f"https://data.sf.gov/resource/{ID}.csv",
                    f"https://data.sf.gov/resource/{ID}.json?$limit=5",
                    f"https://data.sf.gov/api/odata/v4/{ID}",
                    f"https://data.sf.gov/api/views/{ID}.json?row_ids=1"):
            self.assertIsNotNone(fs.aggregate_violation(url), url)

    def test_an_aggregate_needs_group_and_a_small_limit(self):
        self.assertIsNotNone(fs.aggregate_violation(agg(**{"$group": None})))
        self.assertIsNotNone(fs.aggregate_violation(agg(**{"$limit": None})))
        self.assertIsNotNone(fs.aggregate_violation(agg(**{"$limit": "50000"})))

    def test_selecting_a_non_aggregate_column_is_refused(self):
        self.assertIsNotNone(fs.aggregate_violation(agg(**{"$select": "count(*),service_name,status_notes"})))

    def test_grouping_by_an_identifying_column_is_refused(self):
        for key in ("address", "service_request_id", "lat", "point", "media_url", "requester_name"):
            self.assertIsNotNone(fs.aggregate_violation(
                agg(**{"$select": f"{key},count(*)", "$group": key})), key)

    def test_unknown_query_parameters_are_refused(self):
        self.assertIsNotNone(fs.aggregate_violation(agg(**{"$offset": "100"})))

    def test_other_hosts_are_not_restricted_by_this_rule(self):
        self.assertIsNone(fs.aggregate_violation("https://www.sf.gov/data/pothole-response"))

    def test_metadata_reduction_removes_value_samples_only(self):
        body = json.dumps({"name": "311", "columns": [
            {"fieldName": "address", "cachedContents": {"top": [{"item": "1 Main St"}]}},
            {"fieldName": "status", "description": "d"}]}).encode()
        out, note = fs.reduce_body(META, body)
        data = json.loads(out)
        self.assertNotIn("cachedContents", out.decode())
        self.assertEqual([c["fieldName"] for c in data["columns"]], ["address", "status"])
        self.assertIn("rule 7", note)

    def test_other_bodies_are_archived_unchanged(self):
        self.assertEqual(fs.reduce_body("https://www.sf.gov/x", b"<html>"), (b"<html>", ""))


class RunTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "cities" / "t").mkdir(parents=True)
        self.calls = []
        self._fetch, self._robots = fs.fetch, fs.robots_allows
        fs.robots_allows = lambda url: (True, "robots.txt read")
        fs.fetch = self.fake_fetch
        self.responses = {}

    def tearDown(self):
        fs.fetch, fs.robots_allows = self._fetch, self._robots
        shutil.rmtree(self.root)

    def fake_fetch(self, url):
        self.calls.append(url)
        reply = self.responses[url]
        if isinstance(reply, Exception):
            raise reply
        return reply, url, "application/json", 200

    def write_sources(self, urls):
        text = "".join(f"- id: s{i}\n  name: n{i}\n  source_type: open_data_portal\n  url: {u}\n  status: listed\n"
                       for i, u in enumerate(urls))
        (self.root / "cities" / "t" / "sources.yaml").write_text(text)

    def sources(self):
        return validate.load_yaml(self.root / "cities" / "t" / "sources.yaml")

    def run_fetch(self, **kw):
        return fs.run(self.root, "t", set(), 0, **kw)

    def test_metadata_is_archived_reduced_and_named_by_its_own_hash(self):
        self.responses[META] = json.dumps({"columns": [{"fieldName": "x", "cachedContents": {"top": [1]}}]}).encode()
        self.write_sources([META])
        self.assertEqual(self.run_fetch(), 0)
        entry = self.sources()[0]
        self.assertEqual(entry["status"], "fetched")
        archived = (self.root / entry["archive"]).read_bytes()
        self.assertEqual(Path(entry["archive"]).stem, hashlib.sha256(archived).hexdigest())
        self.assertNotIn(b"cachedContents", archived)
        self.assertIn("reduced", entry["notes"])

    def test_a_row_download_is_never_requested(self):
        bad = f"https://data.sf.gov/api/views/{ID}/rows.csv?accessType=DOWNLOAD"
        self.write_sources([bad])
        self.run_fetch()
        self.assertEqual(self.calls, [])
        entry = self.sources()[0]
        self.assertEqual(entry["status"], "unavailable")
        self.assertIn("aggregate-only", entry["notes"])

    def test_too_many_rows_are_not_archived(self):
        self.responses[AGG] = json.dumps([{"n": "1"}] * (fs.AGGREGATE_MAX_ROWS + 1)).encode()
        self.write_sources([AGG])
        self.run_fetch()
        entry = self.sources()[0]
        self.assertEqual(entry["status"], "unavailable")
        self.assertFalse(list((self.root / "sources").glob("*")))

    def test_http_429_leaves_the_source_listed_and_stops_that_host(self):
        other = "https://data.sf.gov/api/views/wxyz-9876.json"
        err = urllib.error.HTTPError(META, 429, "Too Many Requests", {}, None)
        self.responses[META] = err
        self.responses[other] = b"{}"
        self.write_sources([META, other])
        self.assertEqual(self.run_fetch(), 2)
        self.assertEqual(self.calls, [META])
        self.assertEqual([s["status"] for s in self.sources()], ["listed", "listed"])

    def test_the_request_limit_leaves_the_rest_listed(self):
        urls = [f"https://data.sf.gov/api/views/aaa{i}-0000.json" for i in range(3)]
        for u in urls:
            self.responses[u] = b"{}"
        self.write_sources(urls)
        self.assertEqual(self.run_fetch(limit=2), 2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([s["status"] for s in self.sources()], ["fetched", "fetched", "listed"])

    def test_retrieved_date_is_the_utc_date(self):
        self.responses[META] = b"{}"
        self.write_sources([META])
        self.run_fetch()
        self.assertEqual(self.sources()[0]["retrieved"], fs.utc_today().isoformat())
        self.assertNotIn("date.today()", Path(fs.__file__).read_text())


    def test_stage_moves_to_fetched_only_when_no_source_is_left_listed(self):
        status = self.root / "cities" / "t" / "status.yaml"
        status.write_text("stage: discovered\nblocked_on: null\nlast_run: null\n")
        urls = [f"https://data.sf.gov/api/views/aaa{i}-0000.json" for i in range(2)]
        for u in urls:
            self.responses[u] = b"{}"
        self.write_sources(urls)
        self.run_fetch(limit=1)
        self.assertIn("stage: discovered", status.read_text())
        self.run_fetch(limit=1)
        self.assertIn("stage: fetched", status.read_text())

    def test_stage_is_left_alone_when_it_is_not_discovered(self):
        status = self.root / "cities" / "t" / "status.yaml"
        status.write_text("stage: assessed\nblocked_on: null\nlast_run: null\n")
        self.responses[META] = b"{}"
        self.write_sources([META])
        self.run_fetch()
        self.assertIn("stage: assessed", status.read_text())


    def test_a_network_error_or_a_5xx_leaves_the_source_listed_but_a_404_does_not(self):
        urls = [f"https://www.sf.gov/p{i}" for i in range(3)]
        self.responses[urls[0]] = urllib.error.URLError("temporary failure in name resolution")
        self.responses[urls[1]] = urllib.error.HTTPError(urls[1], 502, "Bad Gateway", {}, None)
        self.responses[urls[2]] = urllib.error.HTTPError(urls[2], 404, "Not Found", {}, None)
        self.write_sources(urls)
        self.assertEqual(self.run_fetch(), 2)
        self.assertEqual([s["status"] for s in self.sources()], ["listed", "listed", "unavailable"])

    def test_an_unreadable_robots_file_leaves_the_source_listed(self):
        fs.robots_allows = lambda url: (False, fs.TRANSIENT)
        self.write_sources(["https://www.sf.gov/p"])
        self.assertEqual(self.run_fetch(), 2)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.sources()[0]["status"], "listed")

    def test_robots_disallow_is_still_unavailable(self):
        fs.robots_allows = lambda url: (False, "robots.txt read")
        self.write_sources(["https://www.sf.gov/p"])
        self.run_fetch()
        self.assertEqual(self.sources()[0]["status"], "unavailable")


    def test_a_grouped_query_gets_the_long_timeout_and_other_urls_the_short_one(self):
        self.assertEqual(fs.timeout_for(AGG), fs.QUERY_TIMEOUT)
        self.assertEqual(fs.timeout_for(META), fs.TIMEOUT)
        self.assertEqual(fs.timeout_for("https://www.sf.gov/x"), fs.TIMEOUT)
        self.assertGreater(fs.QUERY_TIMEOUT, fs.TIMEOUT)

    def test_a_timeout_keeps_the_source_listed_and_is_retried_at_most_three_times(self):
        self.responses[META] = TimeoutError("timed out")
        self.write_sources([META])
        for attempt in (1, 2):
            self.assertEqual(self.run_fetch(), 2)
            entry = self.sources()[0]
            self.assertEqual(entry["status"], "listed")
            self.assertEqual(entry["notes"].count(fs.RETRY_MARK), attempt)
        self.assertEqual(self.run_fetch(), 0)
        entry = self.sources()[0]
        self.assertEqual(entry["status"], "unavailable")
        self.assertIn("repeated temporary failures", entry["notes"])

    def test_the_other_temporary_failures_count_toward_the_same_cap(self):
        fs.robots_allows = lambda url: (False, fs.TRANSIENT)
        self.write_sources(["https://www.sf.gov/p"])
        self.run_fetch()
        self.run_fetch()
        self.run_fetch()
        self.assertEqual(self.sources()[0]["status"], "unavailable")

    def test_one_slow_source_does_not_stop_the_others_being_archived(self):
        other = "https://data.sf.gov/api/views/wxyz-9876.json"
        self.responses[META] = TimeoutError("timed out")
        self.responses[other] = b"{}"
        self.write_sources([META, other])
        self.assertEqual(self.run_fetch(), 2)
        self.assertEqual([s["status"] for s in self.sources()], ["listed", "fetched"])


if __name__ == "__main__":
    unittest.main()
