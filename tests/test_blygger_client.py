"""Tests for scripts/blygger_client. Run: python3 -I tests/test_blygger_client.py -v

No network: the client gets a fake opener, and push_drafts gets a fake client. Fixtures are
synthetic and built in a temp directory.
"""
import io
import json
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from blygger_client import (  # noqa: E402
    RenderError, StudioClient, StudioError, WritePathUnavailable, publish_allowed, to_blyg_markdown,
)
from blygger_client.client import token_expiry  # noqa: E402
from blygger_client import push_drafts  # noqa: E402

SHA = "a" * 64
DRAFT = f"""---
slug: testville
version: {{v}}
---
# Testville

Testville logged 5,307 requests[^2] and spent $4 million on paving[^1]. See [^2] again.

| Year | Blocks |
|------|--------|
| 2025 | 12 blocks[^1] |

[^1]: Spending. Source: <https://example.org/q?$select=a&$where=b>, retrieved 2026-11-01. Archived copy: `sources/{SHA}.json`.
[^2]: Requests. Source: <https://example.org/r>, retrieved 2026-11-01. Archived copy: `sources/{SHA}.csv`.
"""


class RenderTests(unittest.TestCase):
    def test_footnotes_become_numbered_notes_in_reference_order(self):
        front, md = to_blyg_markdown(DRAFT.format(v=1))
        self.assertIn("slug: testville", front)
        self.assertNotIn("---", md.split("\n")[0])
        self.assertTrue(md.startswith("# Testville"))
        self.assertNotIn("[^", md)
        # [^2] is referenced first, so it becomes note 1.
        self.assertIn("requests\\[1\\] and spent \\$4 million on paving\\[2\\]", md)
        self.assertIn("## Notes", md)
        notes = md.split("## Notes", 1)[1]
        self.assertLess(notes.index("**\\[1\\]** Requests."), notes.index("**\\[2\\]** Spending."))
        self.assertIn("| 2025 | 12 blocks\\[2\\] |", md)

    def test_every_source_url_and_archive_survives(self):
        _, md = to_blyg_markdown(DRAFT.format(v=1))
        self.assertIn("<https://example.org/q?$select=a&$where=b>", md)  # dollars in a URL stay
        self.assertIn(f"`sources/{SHA}.json`", md)
        self.assertIn(f"`sources/{SHA}.csv`", md)

    def test_archive_base_links_archives(self):
        _, md = to_blyg_markdown(DRAFT.format(v=1), archive_base="https://h.example/blob/main")
        self.assertIn(f"[`sources/{SHA}.json`](https://h.example/blob/main/sources/{SHA}.json)", md)

    def test_missing_definition_stops(self):
        with self.assertRaises(RenderError):
            to_blyg_markdown("# T\n\nA number[^9].\n")

    def test_unreferenced_definition_stops(self):
        with self.assertRaises(RenderError):
            to_blyg_markdown("# T\n\nNo refs.\n\n[^1]: Orphan note.\n")

    def test_control_character_refused(self):
        with self.assertRaises(RenderError):
            to_blyg_markdown("# T\n\nBad \x07 bell.\n")

    def test_code_spans_keep_dollars(self):
        _, md = to_blyg_markdown("# T\n\nUse `$limit` and pay $5.\n")
        self.assertIn("`$limit`", md)
        self.assertIn("pay \\$5.", md)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        status, payload = self.responses.pop(0)
        raw = json.dumps(payload).encode()
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(raw))
        return FakeResponse(raw)


class ClientTests(unittest.TestCase):
    TOKEN = "secret-token-value"

    def client(self, responses):
        opener = FakeOpener(responses)
        return StudioClient("https://s.example/api/", self.TOKEN, opener=opener), opener

    def test_create_draft_sends_thread_with_bearer_token(self):
        c, op = self.client([(201, {"id": "x1", "revision": 1, "status": "draft", "version": 0})])
        item = c.create_draft("# Hi\n")
        req = op.requests[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, "https://s.example/api/items")
        self.assertEqual(req.get_header("Authorization"), f"Bearer {self.TOKEN}")
        self.assertIn("PotholePortolanBot", req.get_header("User-agent"))
        self.assertEqual(json.loads(req.data), {"kind": "thread", "contentMd": "# Hi\n"})
        self.assertEqual(item["id"], "x1")

    def test_update_sends_expected_revision(self):
        c, op = self.client([(200, {"id": "x1", "revision": 4})])
        c.update_draft("x1", "# New\n", 3)
        self.assertEqual(op.requests[0].get_method(), "PATCH")
        self.assertEqual(json.loads(op.requests[0].data), {"contentMd": "# New\n", "expectedRevision": 3})

    def test_list_always_passes_a_limit(self):
        c, op = self.client([(200, {"items": [], "page": {"limit": 50, "nextCursor": None}})])
        c.list_items()
        self.assertIn("limit=50", op.requests[0].full_url)

    def test_errors_carry_code_and_never_the_token(self):
        c, _ = self.client([(409, {"code": "revision-conflict", "message": "stale"})])
        with self.assertRaises(StudioError) as cm:
            c.update_draft("x1", "t", 1)
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(cm.exception.code, "revision-conflict")
        self.assertNotIn(self.TOKEN, str(cm.exception))
        self.assertNotIn(self.TOKEN, repr(c))

    def test_publish_refused_outside_permitted_phases(self):
        c, op = self.client([])
        perms = {"phase": 2, "publish": {"agent_may_publish_in_phases": [3, 4]}}
        self.assertFalse(publish_allowed(perms))
        with self.assertRaises(WritePathUnavailable):
            c.publish("x1", 1, permissions=perms)
        with self.assertRaises(WritePathUnavailable):
            c.publish("x1", 1)
        self.assertEqual(op.requests, [])

    def test_publish_allowed_in_phase_three(self):
        c, op = self.client([(200, {"id": "x1", "version": 1})])
        perms = {"phase": 3, "publish": {"agent_may_publish_in_phases": [3, 4]}}
        c.publish("x1", 2, note="first version", permissions=perms)
        self.assertEqual(json.loads(op.requests[0].data), {"expectedRevision": 2, "note": "first version"})


class FakeClient:
    def __init__(self):
        self.items, self.calls = {}, []

    def create_draft(self, md, kind="thread"):
        self.calls.append(("create", kind))
        item = {"id": f"id{len(self.items) + 1}", "revision": 1, "status": "draft", "version": 0, "contentMd": md}
        self.items[item["id"]] = item
        return item

    def get_item(self, item_id):
        self.calls.append(("get", item_id))
        return dict(self.items[item_id])

    def update_draft(self, item_id, md, rev):
        self.calls.append(("update", item_id, rev))
        if rev != self.items[item_id]["revision"]:
            raise StudioError(409, "revision-conflict", "stale")
        self.items[item_id].update(contentMd=md, revision=rev + 1)
        return dict(self.items[item_id])

    def publish(self, *a, **k):
        raise AssertionError("push_drafts must never publish")


class PushDraftsTests(unittest.TestCase):
    API = "https://s.example/api"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "queue" / "drafts").mkdir(parents=True)
        (self.root / "permissions.yaml").write_text("phase: 2\n")
        self.logs = []

    def tearDown(self):
        shutil.rmtree(self.root)

    def write(self, v, text=None):
        (self.root / "queue" / "drafts" / f"testville-v{v}.md").write_text(text or DRAFT.format(v=v))

    def run_push(self, client, **kw):
        return push_drafts.plan_and_push(self.root, client, self.API, now="2026-11-20T00:00:00Z",
                                         log=self.logs.append, **kw)

    def ledger(self):
        return push_drafts.load_ledger(self.root)

    def test_creates_then_skips_unchanged_then_updates_new_version(self):
        fc = FakeClient()
        self.write(1)
        self.assertEqual(self.run_push(fc), 0)
        entry = self.ledger()["items"]["testville"]
        self.assertEqual((entry["item_id"], entry["dossier_version"]), ("id1", 1))
        self.assertEqual(self.ledger()["studio"], "https://s.example/")

        self.assertEqual(self.run_push(fc), 0)
        self.assertEqual([c[0] for c in fc.calls], ["create"])  # unchanged: nothing sent

        self.write(2, DRAFT.format(v=2).replace("Testville logged", "Testville now logged"))
        self.assertEqual(self.run_push(fc), 0)
        self.assertEqual(fc.calls[-1], ("update", "id1", 1))
        self.assertEqual(self.ledger()["items"]["testville"]["dossier_version"], 2)
        self.assertNotIn("$4", fc.items["id1"]["contentMd"].replace("\\$4", ""))

    def test_dry_run_sends_nothing_and_writes_nothing(self):
        self.write(1)
        self.assertEqual(self.run_push(None, dry_run=True), 0)
        self.assertFalse((self.root / push_drafts.LEDGER).exists())
        self.assertIn("would create a draft", self.logs[0])

    def test_render_failure_sends_nothing(self):
        fc = FakeClient()
        self.write(1, "# T\n\nA number[^9].\n")
        self.assertEqual(self.run_push(fc), 1)
        self.assertEqual(fc.calls, [])

    def test_withdrawn_item_left_alone(self):
        fc = FakeClient()
        self.write(1)
        self.run_push(fc)
        fc.items["id1"]["status"] = "withdrawn"
        self.write(2, DRAFT.format(v=2).replace("Testville logged", "Changed"))
        self.assertEqual(self.run_push(fc), 0)
        self.assertNotIn("update", [c[0] for c in fc.calls])

    def test_other_studio_refused(self):
        fc = FakeClient()
        self.write(1)
        self.run_push(fc)
        self.write(2, DRAFT.format(v=2).replace("Testville logged", "Changed"))
        errors = push_drafts.plan_and_push(self.root, fc, "https://other.example/api", log=self.logs.append)
        self.assertEqual(errors, 1)
        self.assertIn("REFUSED", self.logs[-1])

    def test_pause_and_phase_refuse(self):
        self.assertIsNone(push_drafts.refuse_reason(self.root))
        (self.root / "permissions.yaml").write_text("phase: 1\n")
        self.assertIn("phase", push_drafts.refuse_reason(self.root))
        (self.root / "permissions.yaml").write_text("phase: 2\n")
        (self.root / "PAUSE").write_text("")
        self.assertIn("PAUSE", push_drafts.refuse_reason(self.root))

    def test_ignores_files_that_are_not_versioned_drafts(self):
        (self.root / "queue" / "drafts" / "README.md").write_text("notes")
        self.write(1)
        self.write(3)
        found = push_drafts.latest_drafts(self.root)
        self.assertEqual(list(found), ["testville"])
        self.assertEqual(found["testville"][0], 3)


def fake_jwt(claims):
    import base64

    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature"


class TokenExpiryTests(unittest.TestCase):
    NOW = 1_800_000_000

    def test_reads_exp_without_verifying(self):
        self.assertEqual(token_expiry(fake_jwt({"exp": self.NOW, "scope": "owner:draft"})), self.NOW)

    def test_unreadable_tokens_give_none(self):
        for bad in ("", "opaque-token", "a.b.c", fake_jwt({"scope": "x"}), fake_jwt({"exp": "soon"})):
            self.assertIsNone(token_expiry(bad), bad)

    def test_renewal_window_is_seven_days(self):
        day = 86400
        far = push_drafts.token_status(fake_jwt({"exp": self.NOW + 20 * day}), now=self.NOW)
        near = push_drafts.token_status(fake_jwt({"exp": self.NOW + 7 * day}), now=self.NOW)
        gone = push_drafts.token_status(fake_jwt({"exp": self.NOW - day}), now=self.NOW)
        self.assertEqual((far["days_left"], far["renew"]), (20, False))
        self.assertEqual((near["days_left"], near["renew"]), (7, True))
        self.assertTrue(gone["renew"])
        self.assertRegex(far["expires"], r"^\d{4}-\d{2}-\d{2}$")

    def test_report_writes_status_and_never_the_token(self):
        token = fake_jwt({"exp": self.NOW + 3 * 86400, "sub": "owner"})
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "status.json"
            lines = []
            push_drafts.report_token(token, str(f), now=self.NOW, log=lines.append)
            written = f.read_text()
            self.assertEqual(json.loads(written)["renew"], True)
            self.assertNotIn(token, written)
            self.assertTrue(any("::warning" in line for line in lines))
            self.assertFalse(any(token in line for line in lines))


if __name__ == "__main__":
    unittest.main()
