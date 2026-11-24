# Scheduled funnel (Phase 2)

Written 2026-11-17 for the supervisor's instruction recorded in `queue/approvals/2026-11-17-001-phase2-automation.md`. This page says how the automation works, what you must set up, and what was and was not verified.

## What runs

`.github/workflows/funnel.yml` runs on Monday and Thursday at 10:17 UTC and on demand (Actions tab, "Run workflow"). One run at a time: the concurrency group `funnel` queues a second run and never cancels a running one. Each run does this:

1. Check out the branch with Git LFS objects and no stored credentials. If `PAUSE` exists, say so and stop. Nothing else runs, including the issue update.
2. Copy `checks/`, `scripts/` and `schemas/` to a trusted directory outside the checkout. The guard, the loop and the report run from that copy, so no behavior can edit the code that judges it.
3. Run `scripts/funnel_loop.py`. It stops at once, with a message and exit status 0, if `permissions.yaml` is not phase 2, if `spend.daily_cap_usd` is null, or if the tree is dirty. Otherwise, up to eight times:
   - stop on `PAUSE`, or when today's ledger total has reached `daily_cap_usd`;
   - ask `scripts/next_behavior.py` for the next `(city, behavior)`, or stop if there is none;
   - run that one behavior as its own `claude -p` invocation with only the tools in `registry.yaml`, `--permission-mode dontAsk`, `--max-turns`, and `--max-budget-usd` set to the remaining daily budget;
   - run the ownership guard on the working tree, then the tests, the validator and `run_checks.py --content-only` for the city;
   - on success, write the ledger row and a notebook entry, set `last_run`, and commit with the behavior and city in the message;
   - on failure, save the diff, discard it, record the spend, block the city, and stop.
4. Rebase onto the branch, run the ownership guard over the commit range, and push. If the guard fails, nothing is pushed.
5. Rewrite the body of the one open issue labelled `funnel-summary` (drafts, proposals and blocked cities waiting on you, and the cap) and add a comment for this run (what ran, results, cost).
6. Upload the diff of any discarded behavior as an artifact, and fail the job if a behavior failed.

The loop stops at the first failure, block, or draft, so a run that queues a draft ends there. The next scheduled run takes the next city.

## Stages

`cities/<slug>/status.yaml` holds `stage`, `blocked_on` and `last_run` (schema: `schemas/status.schema.json`). The stage names the last behavior whose output is still current.

| Stage | Next behavior | Set by |
|---|---|---|
| proposed (no folder) | scout | |
| scouted | discover | scout |
| discovered | fetch | discover, or any behavior that lists a new source |
| fetched | extract | `fetch_source.py`, when no source is left listed |
| extracted | assess | extract |
| assessed | draft | assess |
| drafted | publish (queues the draft in Phase 2) | draft |
| queued | none: waiting for you | publish |
| published | none | you |

A behavior may advance one stage, or send a city back to an earlier one, for example when Extract finds it needs a source that is not archived. It may not skip ahead, and a behavior that changes nothing and records no block counts as a failure.

`blocked_on` is null, or `{type, ref, note}`. A city that is blocked is skipped. A `proposal` block lifts by itself when a file in `queue/approvals/` lists the proposal id under `resolves:` in its front matter. Every other block (`supervisor`, `external`, `run_failure`) stays until you edit `status.yaml`. A failed behavior writes `run_failure`.

## What you must set up

1. **Secret `ANTHROPIC_API_KEY`** (Settings, Secrets and variables, Actions). It is the secret name the Claude Code GitHub Actions documentation uses, and the CLI uses it in non-interactive mode. It is the only secret. `GITHUB_TOKEN` is created per run. No Blygger token is used or injected in Phase 2.
2. **Settings, Actions, General, Workflow permissions:** "Read and write permissions", so the run may push commits and edit its issue.
3. **Branch protection on the default branch,** if you have any, must let `github-actions[bot]` push, or the push step fails after the work is done. If you want every run reviewed instead, the push step can be changed to push a branch and open a pull request. Scheduled runs only ever run the workflow as it is on the default branch.
4. **`spend.daily_cap_usd`** in `permissions.yaml`, and `identities.owner`. Until the cap is a number every run exits at once with "daily_cap_usd is null".
5. **Git LFS is not applied yet.** See "Git LFS and the archive hash check" below for why and for the commands you run. Until then `sources/` stays in ordinary git, and `lfs: true` in the workflow's checkout does nothing.
6. **Confirm the tier-to-model map** in `registry.yaml` (`model_tiers`: small haiku, medium sonnet, large opus). It is a placeholder, and the large tier is the expensive one.

To stop everything, create a file named `PAUSE` at the repo root and push it.

## Cost, ledger and the daily cap

The workflow, not the agent, appends `ledger/costs.csv` from each invocation's `total_cost_usd` and `usage`, one row per invocation, with the city, turns, session and cache tokens in the `note` column. The agent is told not to touch the ledger, and the ownership guard fails a run if it does. Today's total is the sum of `usd` over rows stamped with today's UTC date. Rows without a cost, such as the interactive sessions already in the file, count as zero, so interactive spend that was never recorded does not count against the cap. A result with no `total_cost_usd` stops the loop, because the cap could not be enforced. `total_cost_usd` is a client-side estimate according to the CLI documentation, and a run may finish a little past the cap.

## File ownership

A scheduled run may commit agent-owned paths only. The guard (`checks/check_ownership.py`) fails when a diff touches `CLAUDE.md`, `permissions.yaml`, `registry.yaml`, `checks/`, `schemas/` or `.github/`. I added these paths to the list, each for a stated reason, and you can remove any of them in the `ALSO_PROTECTED` list in that file:

- `queue/approvals/`: a forged approval with `resolves:` would unblock a city.
- `templates/`: CLAUDE.md already says the dossier template needs you.
- `PAUSE`, `.gitattributes`.
- `scripts/fetch_source.py`, `next_behavior.py`, `validate.py`, `funnel_loop.py`, `funnel_report.py`, `blygger_client/`, `requirements.txt`, and the tests of these: the loop and the fetch guard run them, so an agent that could edit them could widen its own access.
- `ledger/costs.csv`, for the agent only, because the workflow writes it.

Extraction and assessment scripts, other tests, `notes.md`, claims, sources, notebook, agenda and proposals stay agent-owned.

## Fetch

`scripts/fetch_source.py` is the only thing the Fetch behavior can run. It checks robots.txt for the host and for every redirect hop, uses an honest user agent, waits three seconds between requests, makes at most 20 requests a run, stops a host on HTTP 429, and leaves a source `listed` for a retry on a network error, a timeout or a 5xx, since those say nothing about the source. On `data.sf.gov` and `data.sfgov.org` it allows only dataset and column metadata and grouped aggregate queries (`$select` with only aggregate calls and the `$group` keys, a `$group`, and a `$limit` of at most 2000), refuses row downloads and identifying group keys, and archives metadata without the `cachedContents` value samples that Socrata embeds, because those hold sample record values (rule 7).

## Git LFS and the archive hash check

**Status: not applied.** I prepared and tested the migration locally, but could not push it. This session's network policy answers Git LFS's `verify` calls with HTTP 403, so the LFS objects could not be uploaded, and GitHub rejects a push whose LFS pointers have no uploaded objects. I removed the migration from this branch rather than push pointers without content. The workflow still checks out with `lfs: true` and the archive check still understands pointers, so nothing else changes when you apply it.

**LFS does work with the hash check.** Archives are named by the sha256 of their content, and an LFS pointer's `oid` is the sha256 of the same bytes. I confirmed all ten existing archives satisfy `oid == file name`. The working tree holds real content after a checkout that fetched the LFS objects, which is what `run_checks.py` reads. A checkout without them now fails the archives check with a message that says so, instead of a hash mismatch (`tests/test_checks.py` covers this).

**To apply it, from a machine where LFS works, on this branch after merging:**

```bash
git lfs install
printf '%s\n' 'sources/[0-9a-f]* filter=lfs diff=lfs merge=lfs -text' > .gitattributes
git add .gitattributes && git add --renormalize sources
git commit -m "lfs: store archived sources in Git LFS going forward"
git push
```

This does not rewrite history: the existing blobs stay in earlier commits. `.gitattributes` is on the protected list, so scheduled runs cannot change it. Two costs to weigh first. Your GitHub plan has LFS storage and bandwidth quotas, and every run downloads all LFS objects at checkout, so bandwidth grows with the archive (about 18 MB now, mostly two PDFs). And only moving the existing blobs out of history, with `git lfs migrate import --everything --include="sources/**"` and a force-push, would shrink the repository itself.

**If you would rather not use LFS,** the alternative is to keep archives in ordinary git and set a size ceiling in the fetcher. `scripts/fetch_source.py` already caps a download at 50 MB. The archive hash check needs no change either way.

## Verified, and not

Verified here, with Claude Code 2.1.326:

- The CLI flags used (`-p`, `--output-format json`, `--max-turns`, `--model`, `--permission-mode dontAsk`, `--max-budget-usd`, `--allowedTools`, `--disallowedTools`, `--no-session-persistence`) are accepted, and the result is one JSON object with `result`, `is_error`, `subtype`, `num_turns`, `total_cost_usd`, `usage` and `session_id`.
- A scoped `Bash(python3 -I scripts/… *)` rule allows that command and denies any command chained to it, including `&& curl`.
- One real Fetch behavior, run through the loop on a scratch copy of the repository, fetched the two DataSF metadata endpoints (`data.sfgov.org` redirects to `data.sf.gov`), archived reduced copies, advanced the stage, and committed with a ledger row. The same copy showed a row download refused without a request.
- 129 unit tests pass with the loop's own command.

Not verified:

- The workflow has not run on GitHub. It parses as YAML and its invariants are tested, but it was not linted or executed by Actions.
- docs.github.com could not be fetched in this session. So the concurrency queue behavior (a third waiting run), the schedule rules beyond what the Claude Code docs state (default branch only, disabled after 60 days without activity in public repositories), the LFS quotas, and GitHub's refusal of a push that references missing LFS objects are unconfirmed.
- Whether a `data.sf.gov` aggregate query is accepted without an app token. Only metadata endpoints were requested.
- Only Fetch was run live. Scout, Discover, Extract, Assess, Draft and Publish prompts and their `max_turns` are untested with a model, and the turn limits and tier costs are guesses.
- Pushing LFS objects at all: it failed from this session (HTTP 403 on the LFS verify call), so the migration is not applied and its push from the workflow is untested.
- The other behaviors still have an unrestricted shell or web tools. See `queue/proposals/2026-11-17-002-narrow-network-and-shell-access.md`.

## Fetch guards after merging the pilot branch

The Phase 1 pilot and this branch each built a guard for the DataSF portals, and `scripts/fetch_source.py` now applies both. A URL on a host in `AGGREGATE_ONLY_HOSTS` must pass `aggregate_violation` (host level: metadata or grouped aggregates, a `$group`, a row limit) and then `query_policy` (dataset level: a minimum group size of ten cases through `$having`, no identifying field, and `service_details` only when the query is pinned to one subtype). A refusal by either is recorded as `unavailable` with the reason, the same as the existing aggregate-only refusals.

Responses are checked too, because a query can be allowed and still return something it should not:

- A response that has more rows than `AGGREGATE_MAX_ROWS` is not archived.
- On a registered aggregate-only dataset, a text field with more than `MAX_TEXT_DISTINCT` distinct values is held and never written to disk. This is the signature of typed text such as permit applicants' names, which repeats above any group-size minimum.
- Metadata loses its per-column value samples (`cachedContents`) before archiving, on the restricted hosts through `reduce_body` and on any host through `sanitize`.

Choices made in the merge, so they can be revisited:

- A registered dataset may use the `$limit` of its own policy (5000), because it is held to the stricter policy as well. Unregistered datasets keep the 2000 cap. What is archived is capped at 2000 rows either way.
- Dataset metadata is accepted with or without the `.json` suffix, since it is the same endpoint.
- The Scorecard Measures dataset (`kc49-udxn`) may be read as rows, because it holds department performance figures and not records about people. Every other registered dataset is aggregate-only.
- `check_personal_data` treats a dataset description as safe unless it still contains `cachedContents`, and re-applies the group-size and typed-text guards to archived responses from registered datasets.

**Chicago (added 2026-11-24).** `data.cityofchicago.org` is aggregate-only too, with its 311 Service Requests (`v6vf-nfxy`) and the historical 311 Pot Holes Reported (`7as2-ds3y`) registered as grouped counts only, and Chicago's address, coordinate and request-number columns added to the sensitive fields. The dataset ids and column names were registered before Discover ran for Chicago; if Discover finds different ids, those are refused until registered, which is the safe way to be wrong. Tests: `tests/test_fetch_chicago.py`.

Known limits:

- Hosts outside `AGGREGATE_ONLY_HOSTS` get neither guard, only robots.txt, the size cap and the redirect check. A new city's portal has to be added to that set, and each dataset registered in `DATASET_POLICY`, before it is queried; otherwise a Fetch run could archive row-level data before `check_personal_data` rejects it, and the loop would then discard the run.
- The guards only cover Socrata-style endpoints. Other APIs need their own policy.

The tests for the combination are in `tests/test_fetch_merged_guards.py`. Each protection was disabled in turn to confirm that a test catches it.
