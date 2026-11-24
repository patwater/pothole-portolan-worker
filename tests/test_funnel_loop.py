"""Tests for scripts/funnel_loop.py. Run: python3 -I tests/test_funnel_loop.py -v

A fake runner stands in for the model, so no test spends money or touches the network.
Each test builds a throwaway git repository.
"""
import datetime
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import funnel_loop as fl  # noqa: E402
import next_behavior  # noqa: E402

NOW = datetime.datetime(2026, 11, 20, 10, 17, tzinfo=datetime.timezone.utc)
AGENDA = "# Agenda\n\n## Queue\n\n1. alpha: status `proposed`\n2. beta: status `proposed`\n"
NEXT_STAGE = {"proposed": "scouted", "scouted": "discovered", "discovered": "fetched", "fetched": "extracted",
              "extracted": "assessed", "assessed": "drafted", "drafted": "queued"}


def ok_result(cost=0.5, **extra):
    base = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "total_cost_usd": cost,
            "session_id": "s1", "result": "Did the work.\nNext is the following step.",
            "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 5,
                      "cache_creation_input_tokens": 7}}
    base.update(extra)
    return base


class FunnelTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.patches = Path(tempfile.mkdtemp())
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.org")
        self.git("config", "user.name", "t")
        self.write("agenda.md", AGENDA)
        self.write("CLAUDE.md", "brief\n")
        self.write("registry.yaml", (REPO / "registry.yaml").read_text())
        self.write("ledger/costs.csv", "timestamp_utc,behavior,model_tier,input_tokens,output_tokens,usd,note\n"
                   "2026-11-19T09:00:00Z,fetch,small,1,2,3.000000,yesterday\n"
                   "2026-11-20T00:00:00Z,assess,n/a,,,,interactive row without a cost\n")
        self.write("queue/drafts/.gitkeep", "")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.registry = yaml.safe_load((self.root / "registry.yaml").read_text())
        self.permissions = {"phase": 2, "spend": {"daily_cap_usd": 5.0}}
        self.calls = []
        self.verify_ok = (True, "ok")
        self.script = None  # callable(city, behavior, stage) that edits the tree; default advances one stage

    def tearDown(self):
        shutil.rmtree(self.root)
        shutil.rmtree(self.patches)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True).stdout

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def set_status(self, city, stage, blocked=None):
        self.write(f"cities/{city}/status.yaml", yaml.safe_dump(
            {"stage": stage, "blocked_on": blocked, "last_run": None}, sort_keys=False))

    def advance(self, city, behavior, stage):
        self.set_status(city, NEXT_STAGE[stage])
        if NEXT_STAGE[stage] == "queued":
            self.write(f"queue/drafts/{city}-v1.md", "draft\n")

    def runner(self, argv, env, cwd, timeout_s):
        prompt = argv[2]
        behavior = prompt.split()[2]
        city = prompt.split("city ")[1].split(".")[0]
        stage = prompt.split("stage is ")[1].split(" ")[0]
        self.calls.append({"argv": argv, "env": env, "behavior": behavior, "city": city})
        (self.script or self.advance)(city, behavior, stage)
        return self.reply, ""

    reply = None

    def funnel(self, **kw):
        self.reply = self.reply or ok_result()
        return fl.Funnel(self.root, self.registry, self.permissions, self.runner,
                         lambda root, city, pre: self.verify_ok,
                         fl.default_guard(REPO / "checks" / "check_ownership.py"),
                         clock=lambda: NOW, run_id="42", run_url="https://example.org/run/42",
                         patch_dir=self.patches, environ={"ANTHROPIC_API_KEY": "k", "GITHUB_TOKEN": "g",
                                                          "BLYGGER_WRITE_TOKEN": "w", "PATH": "/usr/bin"}, **kw)

    def ledger_rows(self):
        import csv
        with open(self.root / "ledger" / "costs.csv", newline="") as f:
            return list(csv.DictReader(f))

    # ---- early exits ----
    def test_null_cap_exits_early_with_a_clear_message_and_runs_nothing(self):
        self.permissions["spend"]["daily_cap_usd"] = None
        out = self.funnel().run()
        self.assertEqual(out["outcome"], "cap_unset")
        self.assertIn("daily_cap_usd", out["message"])
        self.assertEqual(self.calls, [])

    def test_pause_exits_early_and_runs_nothing(self):
        (self.root / "PAUSE").write_text("")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-q", "-m", "pause"], check=True)
        self.assertEqual(self.funnel().run()["outcome"], "paused")
        self.assertEqual(self.calls, [])

    def test_only_phase_two_runs(self):
        self.permissions["phase"] = 3
        self.assertEqual(self.funnel().run()["outcome"], "wrong_phase")
        self.assertEqual(self.calls, [])

    def test_a_dirty_tree_at_the_start_runs_nothing(self):
        self.write("stray.txt", "x")
        self.assertEqual(self.funnel().run()["outcome"], "dirty")
        self.assertEqual(self.calls, [])

    # ---- the happy path ----
    def test_the_loop_runs_a_city_through_to_a_queued_draft_and_stops(self):
        out = self.funnel().run()
        self.assertEqual(out["outcome"], "draft_queued")
        self.assertEqual([c["behavior"] for c in self.calls],
                         ["scout", "discover", "fetch", "extract", "assess", "draft", "publish"])
        self.assertTrue(all(c["city"] == "alpha" for c in self.calls))
        self.assertEqual(self.git("log", "--format=%s").count("\n"), 8)  # 7 behaviors + base
        self.assertIn("publish: alpha, stage drafted to queued", self.git("log", "-1", "--format=%s"))
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_each_behavior_commit_holds_its_ledger_row_and_notebook_entry(self):
        self.funnel().run()
        rows = [r for r in self.ledger_rows() if r["note"].startswith("city=")]
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows[0]["behavior"], "scout")
        self.assertEqual(rows[0]["usd"], "0.500000")
        self.assertEqual(rows[0]["input_tokens"], "10")
        self.assertIn("run=42", rows[0]["note"])
        book = (self.root / "notebook" / "2026-11-20.md").read_text()
        self.assertIn("scheduled run 42: scout on alpha", book)
        self.assertIn("> Did the work.", book)
        files = self.git("show", "--name-only", "--format=", "HEAD").split()
        self.assertIn("ledger/costs.csv", files)
        self.assertIn("notebook/2026-11-20.md", files)

    def test_last_run_is_recorded_in_status(self):
        self.funnel(max_behaviors=1).run()
        status = yaml.safe_load((self.root / "cities/alpha/status.yaml").read_text())
        self.assertEqual(status["last_run"], {"behavior": "scout", "date": "2026-11-20", "workflow_run": "42"})

    def test_the_run_is_capped_at_max_behaviors(self):
        out = self.funnel(max_behaviors=3).run()
        self.assertEqual(out["outcome"], "max_behaviors")
        self.assertEqual(len(self.calls), 3)

    def test_eight_is_the_default_cap_from_the_registry(self):
        self.assertEqual(self.funnel().max_behaviors, 8)

    def test_nothing_to_do_when_every_city_is_queued(self):
        self.set_status("alpha", "queued")
        self.set_status("beta", "queued")
        self.git("add", "-A"), self.git("commit", "-q", "-m", "s")
        self.assertEqual(self.funnel().run()["outcome"], "nothing_to_do")

    # ---- spend cap ----
    def test_todays_spend_counts_only_todays_rows_and_treats_blank_as_zero(self):
        self.assertEqual(fl.spend_today(self.root, NOW), 0.0)

    def test_the_loop_stops_once_todays_total_reaches_the_cap(self):
        self.permissions["spend"]["daily_cap_usd"] = 1.0
        self.reply = ok_result(cost=0.6)
        out = self.funnel().run()
        self.assertEqual(out["outcome"], "cap_reached")
        self.assertEqual(len(self.calls), 2)
        self.assertAlmostEqual(out["spend_today_usd"], 1.2, places=6)

    def test_earlier_spend_today_counts_against_the_cap(self):
        self.write("ledger/costs.csv", (self.root / "ledger/costs.csv").read_text()
                   + "2026-11-20T01:00:00Z,x,small,,,4.900000,earlier run today\n")
        self.git("add", "-A"), self.git("commit", "-q", "-m", "spend")
        out = self.funnel().run()
        self.assertEqual(out["outcome"], "cap_reached")
        self.assertEqual(len(self.calls), 1)
        argv = self.calls[0]["argv"]
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "0.1000")

    def test_each_invocation_gets_the_remaining_budget(self):
        self.permissions["spend"]["daily_cap_usd"] = 5.0
        self.funnel(max_behaviors=1).run()
        argv = self.calls[0]["argv"]
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "5.0000")

    def test_hitting_the_budget_mid_behavior_stops_without_blocking_the_city(self):
        self.reply = ok_result(subtype="error_max_budget_usd", is_error=True)
        out = self.funnel().run()
        self.assertEqual(out["outcome"], "cap_reached")
        self.assertFalse((self.root / "cities/alpha/status.yaml").exists())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_a_result_without_a_cost_stops_the_loop_and_is_not_trusted(self):
        reply = ok_result()
        del reply["total_cost_usd"]
        self.reply = reply
        out = self.funnel().run()
        self.assertEqual(out["outcome"], "failed")
        self.assertIn("daily cap cannot be enforced", out["message"])
        self.assertEqual(self.ledger_rows()[-1]["usd"], "")
        self.assertFalse((self.root / "cities/alpha").exists())

    # ---- failures ----
    def test_a_behavior_that_edits_a_protected_file_is_discarded_and_the_city_blocked(self):
        def script(city, behavior, stage):
            self.advance(city, behavior, stage)
            self.write("CLAUDE.md", "tampered\n")
            self.write("checks/new.py", "x\n")
        self.script = script
        out = self.funnel().run()
        self.assertEqual(out["outcome"], "failed")
        self.assertEqual((self.root / "CLAUDE.md").read_text(), "brief\n")
        self.assertFalse((self.root / "checks").exists())
        status = yaml.safe_load((self.root / "cities/alpha/status.yaml").read_text())
        self.assertEqual(status["blocked_on"]["type"], "run_failure")
        self.assertEqual(status["stage"], "proposed")
        self.assertTrue(list(self.patches.glob("01-scout-alpha.patch")))
        self.assertEqual(self.ledger_rows()[-1]["behavior"], "scout")  # the spend is still recorded
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_a_blocked_city_is_skipped_by_the_next_run(self):
        self.script = lambda c, b, s: self.write("CLAUDE.md", "tampered\n")
        self.funnel().run()
        choice, skipped = next_behavior.pick(self.root)
        self.assertEqual(choice["city"], "beta")
        self.assertTrue(any("run_failure" in line for line in skipped))

    def test_a_behavior_that_does_not_move_the_stage_fails(self):
        self.script = lambda city, behavior, stage: self.set_status(city, stage)
        self.set_status("alpha", "fetched")
        self.git("add", "-A"), self.git("commit", "-q", "-m", "s")
        out = self.funnel().run()
        self.assertEqual(out["outcome"], "failed")
        self.assertIn("did not change", out["message"])

    def test_a_behavior_may_not_skip_a_stage(self):
        self.script = lambda city, behavior, stage: self.set_status(city, "assessed")
        out = self.funnel().run()
        self.assertEqual(out["outcome"], "failed")
        self.assertIn("jumped", out["message"])

    def test_a_behavior_may_send_the_city_back_a_stage(self):
        self.set_status("alpha", "fetched")
        self.git("add", "-A"), self.git("commit", "-q", "-m", "s")
        self.script = lambda city, behavior, stage: self.set_status(city, "discovered")
        out = self.funnel(max_behaviors=1).run()
        self.assertEqual(out["outcome"], "max_behaviors")
        self.assertEqual(out["steps"][0]["stage_after"], "discovered")

    def test_a_blocked_status_stops_the_loop_without_failing(self):
        self.script = lambda city, behavior, stage: self.set_status(
            city, stage, {"type": "proposal", "ref": "p1", "note": "needs a decision"})
        self.set_status("alpha", "extracted")
        self.git("add", "-A"), self.git("commit", "-q", "-m", "s")
        out = self.funnel().run()
        self.assertEqual(out["outcome"], "blocked")
        self.assertEqual(len(self.calls), 1)

    def test_failed_verification_discards_the_change(self):
        self.verify_ok = (False, "tests failed:\nboom")
        out = self.funnel().run()
        self.assertEqual(out["outcome"], "failed")
        self.assertIn("tests failed", out["message"])
        self.assertFalse((self.root / "cities/alpha").exists() and
                         not (self.root / "cities/alpha/status.yaml").read_text().count("run_failure"))

    def test_an_error_result_is_a_failure(self):
        self.reply = ok_result(subtype="error_max_turns", is_error=True)
        out = self.funnel().run()
        self.assertEqual(out["outcome"], "failed")
        self.assertIn("error_max_turns", out["message"])

    def test_a_missing_result_fails_without_blocking_the_city(self):
        runner_calls = []

        def runner(argv, env, cwd, timeout_s):
            runner_calls.append(1)
            return None, "timed out after 1800 seconds"
        f = self.funnel()
        f.runner = runner
        out = f.run()
        self.assertEqual(out["outcome"], "failed")
        self.assertFalse((self.root / "cities/alpha/status.yaml").exists())

    def test_a_queued_status_without_a_draft_file_fails(self):
        self.set_status("alpha", "drafted")
        self.git("add", "-A"), self.git("commit", "-q", "-m", "s")
        self.script = lambda city, behavior, stage: self.set_status(city, "queued")
        self.assertEqual(self.funnel().run()["outcome"], "failed")

    # ---- the invocation itself ----
    def test_fetch_has_only_the_fetch_script_and_no_web_tool(self):
        argv, creds = fl.build_invocation(self.registry, "fetch", "alpha", "discovered", "2026-11-20", 1.0)
        allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
        self.assertEqual(allowed, ["Read", "Bash(python3 -I scripts/fetch_source.py *)"])
        denied = argv[argv.index("--disallowedTools") + 1:]
        for tool in ("WebFetch", "WebSearch"):
            self.assertIn(tool, denied)
        self.assertEqual(creds, [])

    def test_the_invocation_uses_the_documented_flags(self):
        argv, _ = fl.build_invocation(self.registry, "extract", "alpha", "fetched", "2026-11-20", 1.0)
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertIn("alpha", argv[2])
        for flag in ("--output-format", "--max-turns", "--model", "--permission-mode", "--max-budget-usd", "--allowedTools"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[argv.index("--max-turns") + 1], "60")
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")

    def test_the_invocation_sees_only_the_api_key_and_its_registry_credentials(self):
        env = fl.build_env([], {"ANTHROPIC_API_KEY": "k", "GITHUB_TOKEN": "g", "BLYGGER_WRITE_TOKEN": "w",
                                "PATH": "/bin", "AWS_SECRET_ACCESS_KEY": "a"})
        self.assertEqual(env, {"ANTHROPIC_API_KEY": "k", "PATH": "/bin"})
        env = fl.build_env(["BLYGGER_READ_TOKEN"], {"ANTHROPIC_API_KEY": "k", "BLYGGER_READ_TOKEN": "r"})
        self.assertEqual(env["BLYGGER_READ_TOKEN"], "r")

    def test_no_funnel_behavior_is_given_a_blygger_write_credential(self):
        for name, spec in self.registry["behaviors"].items():
            self.assertNotIn("BLYGGER_WRITE_TOKEN", spec.get("credentials") or [], name)

    def test_a_ledger_row_without_a_trailing_newline_does_not_corrupt_the_next_row(self):
        path = self.root / "ledger" / "costs.csv"
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
        fl.append_ledger(self.root, NOW, "scout", "small", None, None, "n")
        rows = self.ledger_rows()
        self.assertEqual(rows[-1]["behavior"], "scout")
        self.assertEqual(len(rows), 3)


    # ---- a fetch cut short by a temporary failure keeps its progress ----
    def write_sources(self, city, statuses):
        self.write(f"cities/{city}/sources.yaml", yaml.safe_dump(
            [{"id": f"s{i}", "name": "n", "source_type": "other", "url": f"https://example.org/{i}", "status": st,
              **({"retrieved": "2026-11-20", "archive": "sources/" + "a" * 64 + ".json", "robots_checked": True}
                 if st == "fetched" else {})} for i, st in enumerate(statuses)], sort_keys=False))

    def discovered_city(self):
        self.set_status("alpha", "discovered")
        self.write_sources("alpha", ["listed", "listed"])
        self.git("add", "-A"), self.git("commit", "-q", "-m", "s")

    def test_a_fetch_that_fetched_some_and_left_others_listed_is_kept_and_does_not_block_the_city(self):
        self.discovered_city()

        def partial(city, behavior, stage):
            self.write_sources(city, ["fetched", "listed"])  # what fetch_source.py leaves after a timeout
        self.script = partial
        out = self.funnel().run()
        self.assertEqual(out["outcome"], "fetch_incomplete")
        self.assertEqual(len(self.calls), 1)
        statuses = [s["status"] for s in yaml.safe_load((self.root / "cities/alpha/sources.yaml").read_text())]
        self.assertEqual(statuses, ["fetched", "listed"])
        status = yaml.safe_load((self.root / "cities/alpha/status.yaml").read_text())
        self.assertIsNone(status["blocked_on"])
        self.assertEqual(status["stage"], "discovered")
        self.assertIn("fetch: alpha", self.git("log", "-1", "--format=%s"))
        self.assertEqual(self.ledger_rows()[-1]["behavior"], "fetch")
        self.assertEqual(next_behavior.pick(self.root)[0]["behavior"], "fetch")  # the next run retries it
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_a_fetch_that_changed_nothing_is_still_a_failure(self):
        self.discovered_city()
        self.script = lambda city, behavior, stage: None  # the script never ran
        out = self.funnel().run()
        self.assertEqual(out["outcome"], "failed")
        status = yaml.safe_load((self.root / "cities/alpha/status.yaml").read_text())
        self.assertEqual(status["blocked_on"]["type"], "run_failure")

    def test_a_fetch_with_nothing_left_listed_must_still_advance_the_stage(self):
        self.discovered_city()
        self.script = lambda city, behavior, stage: self.write_sources(city, ["fetched", "fetched"])
        self.assertEqual(self.funnel().run()["outcome"], "failed")

    def test_only_fetch_gets_this_allowance(self):
        self.set_status("alpha", "fetched")
        self.write_sources("alpha", ["listed"])
        self.git("add", "-A"), self.git("commit", "-q", "-m", "s")
        self.script = lambda city, behavior, stage: self.write_sources(city, ["fetched"])
        self.assertEqual(self.funnel().run()["outcome"], "failed")  # extract changed sources but not the stage


if __name__ == "__main__":
    unittest.main()
