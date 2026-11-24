# Blygger: how a blyg is hosted, and how this project writes to one

Researched on 2026-11-19 from blygger.org and the Blygger Studio repository's own documentation. Nothing was written or published during the research.

**This replaces the Phase 0 version of this page, which said no write operation exists. That was wrong.** The Phase 0 conclusion came from a connector whose grant could only read, so it listed only reads. The Studio has a documented API for creating drafts, publishing and versioning, with separate scopes for each (below).

Sources read:

- https://blygger.org/start/ (how to host a blyg)
- https://blygger.org/spec/0.4/ (the protocol; the "living spec" is the highest-numbered version)
- https://raw.githubusercontent.com/blygger/blygger-studio/main/README.md (the reference client)
- https://raw.githubusercontent.com/blygger/blygger-studio/main/docs/client-access.md (tokens and scopes)
- https://raw.githubusercontent.com/blygger/blygger-studio/main/docs/api.md (the owner API)
- https://raw.githubusercontent.com/blygger/blygger-studio/main/docs/auth-deployment-hardening.md (limits)

## Two layers

**The protocol** is a set of static files plus RSS. The spec requires that "the entire surface MUST be servable as plain static files", so a blyg can live on a plain file host. The required files, relative to the blyg's origin, are `blyg.json` (manifest), `feed.xml` (RSS 2.0), `items/index.json` (archive index), `items/{id}.json` (one document per item, with `content_md`, `content_html`, `content_hash` and a version number), `items/{id}/v{n}.json` (pinned versions) and `media/…`. Item ids are 128 random bits written as 26 lowercase Crockford base32 characters. `content_md` must not contain control characters other than tab, line feed and carriage return, and should be in Unicode Normalization Form C. The spec says nothing about which Markdown features are supported (footnotes, tables), because the publisher renders `content_html` itself.

**Blygger Studio** is the reference client: a Cloudflare Worker that publishes, subscribes and receives mentions, while its published output stays static files. Its install is `git clone` of the Studio repository, then `npm ci`, `npx wrangler login`, `npm run init` and `npm run deploy`. `init` asks for a Cloudflare account and domain, creates a D1 database (binding `DB`) and an R2 bucket (binding `MEDIA`), applies migrations and sets the secrets `OWNER_PASSWORD` and `COOKIE_SECRET`. The blyg is then at a subdomain such as `https://blyg.example.com/`, with the Studio at `/studio`. **A Studio is its own application with its own `wrangler.jsonc`, installed from a clone of the Studio repository. It cannot be deployed from this repository.**

## The write path

Authentication is a bearer token. A Studio owner creates manual tokens, or approves OAuth grants, on the Studio's access page, and each token carries explicit scopes. Scopes do not imply each other.

| Scope | Allows |
|-------|--------|
| `owner:read` | Private reads: drafts, reading material, media, settings and history |
| `owner:draft` | Creating, editing, restoring and deleting drafts, media uploads, preview and configured AI generation |
| `owner:publish` | Publication, withdrawal, refresh, permanent pins, public response display and media deletion |
| `owner:manage` | Settings, subscriptions, signals, collections and response moderation |
| `reading:state` | Marking reading entries read or unread |

The REST routes below are under `{mount}/api`, and the same operations are available over MCP at `{mount}/studio/mcp`. A call without the needed scope gets an HTTP 403 scope challenge. The owner can download the deployed OpenAPI contract at `{mount}/api/openapi.json`.

| Need | Route | Scope |
|------|-------|-------|
| Create a draft | `POST /items` | `owner:draft` |
| Edit a draft | `PATCH /items/{id}` | `owner:draft` |
| Publish (a new version of the same item each time) | `POST /items/{id}/publish`, with an optional publication note | `owner:publish` |
| Withdraw | `POST /items/{id}/withdraw` | `owner:publish` |
| Pin a version permanently | `PUT /items/{id}/versions/{version}/pin` | `owner:publish` |
| Read items and versions | `GET /items`, `GET /items/{id}`, `GET /items/{id}/versions` | `owner:read` |
| Read mentions | `GET /mentions?direction=inbound` or `outbound` | `owner:read` |
| Read capabilities | `GET /capabilities` | any valid grant |

The spec says a publish event increases an item's `version` by exactly one, and that ids never change across versions, which is what makes "publish a new version that says what changed" possible. Writes are rate limited (the hardening document lists defaults such as 120 owner writes and 60 delegated writes per minute).

## What this project's supervisor already has

The supervisor's own blyg is at https://blyg.pioneeringspirit.xyz, with the Studio at `/studio`, the API at `/api`, the MCP endpoint at `/studio/mcp` and the auth issuer at `/studio/auth`. The connector used in Phase 0 reported contract version 5.10.0 with 39 operations, all of them reads. That is what a read-scoped grant shows, and it is not evidence that the write operations are missing. The project's own blyg for Pothole Portolan does not exist yet: nothing in this repository is a Studio, and whether it will be one is the open decision in proposal `2026-11-19-002`.

## How the scopes fit the project

- **Reader intake** needs mentions and interactions, which need `owner:read`. That scope also reads drafts, settings and history, which is broader than intake needs, but it has no write power. It satisfies "read-only token, no publish credential".
- **Phase 2** (drafts only) could use an `owner:draft` token to put a draft in the Studio without making it public. It does not need `owner:publish`.
- **Phase 3** (gated publishing) is the first phase that needs `owner:publish`, and it should be a separate token from the draft token, so that a run that only drafts cannot publish.
- `registry.yaml` currently names two credentials, `BLYGGER_READ_TOKEN` and `BLYGGER_WRITE_TOKEN`. A cleaner split is `BLYGGER_READ_TOKEN`, `BLYGGER_DRAFT_TOKEN` and `BLYGGER_PUBLISH_TOKEN`. `registry.yaml` is supervisor-owned, so that change is in the proposal and not made.

## Still unknown or unverified

- The exact request body for `POST /items` (see "Create an item" in `docs/api.md`) and the content limits.
- Whether the Studio's Markdown renderer supports footnotes and tables. The rendered Pothole Portolan drafts use both.
- Whether a token can be restricted to one resource beyond its scopes (the client-access document mentions "resource-bound" tokens).
- Whether `verifiedAt` on a mention is strong enough to map a mention to the owner, trusted or public tier in `permissions.yaml`.
- How a scheduled run authenticates in practice: tokens would be stored as GitHub Actions secrets, which the supervisor must create.
- The Studio API is called private and its contract changes with releases (the project's own blyg announced contract 5.13.0 on 2026-11-19, while the supervisor's Studio reported 5.10.0). A client has to be tested against the supervisor's deployed version.

## Paging and cost

`listItems` called without a limit returned about 194,000 characters in one response. Every client call must pass a limit and follow cursors where the operation supports them.

## A working example: how the World Machines blyg is automated

Researched on 2026-11-19 at the supervisor's request, from https://blyg.worldmachines.org/ (its `blyg.json`, `items/index.json` and 26 item documents) and from the public repository `worldmachines/worldmachines` on GitHub (files `blyg-shell/README.md`, `.github/workflows/blyg-sync.yml`, `.github/workflows/blyg-front.yml`, `website/scripts/blyg_sync.py`, `website/scripts/blyg_series.py`). Nothing was written to either site, and no code was copied, because that repository has no license file.

**It is a stock Blygger Studio driven by GitHub Actions.** The manifest names `blygger-studio/0.64.1` as the generator and declares a `webmention` endpoint, `/studio` redirects to a sign-in, and `/api/capabilities` answers 401 without a token. The shell README says the Studio is deployed from a separate private checkout, unforked, and that a small second Worker sits in front of it only to add the site's navigation bar.

**Publishing is a script, not a person.** Two scheduled workflows in the public repository run stdlib Python scripts that call the Studio REST API with a bearer token held as a GitHub Actions secret:

- `blyg-sync.yml`, at 06:00 and 18:00 UTC, collects posts that members file into their own hoppers, files them into the World Machines `members` hopper, and publishes a digest of them.
- `blyg-front.yml`, at 06:30 and 18:30 UTC, writes one series post a day (a weekday rotation of six formats) and republishes the front-page thread. Its only git write is `tools/blyg-front/history.json`, which records what was featured so that a subject is not reused within 90 days.

Each post follows the same sequence of calls: `POST /items` with `kind: thread` and the Markdown creates a draft, `POST /items/{id}/generate` asks the Studio to fill each `[TK]` scope, and `POST /items/{id}/publish` publishes it, after which the item is filed into a hopper. Item writes carry `expectedRevision`, and a 409 revision conflict is re-read and retried once. The item documents agree with this: each recent post was created and then published about ten seconds later, as version 1.

**The AI writes only inside marked scopes.** The script assembles everything deterministic itself: headings, links, the list of source posts, and quotes as partial transclusions whose text the Studio checks is a verbatim substring of the target. Only the `[TK]` scopes are written by a model, which is the Studio's own configured model (`@cf/google/gemma-4-26b-a4b-it` on Cloudflare Workers AI). Each published item then carries a `generated` field naming the model, the time, and every source item and version the scope drew on. The front-page thread is at version 26, and every version has a changelog note such as "minor: today's featured note is Legibility".

**Operational limits the scripts work around:**

- Studio REST tokens expire after at most 30 days, so a person has to mint a new one and update the secret each month. The scripts fail with renewal instructions on a 401 or 403.
- Studio AI generation has a daily budget per grant (the scripts cite 10 calls a grant and 40 for the whole blyg, and 20 for the digest token). A 429 from `generate` is that budget, is never retried, and leaves the draft for the next run to resume.
- The scripts say they speak Studio 0.51's private API (contract 5.2.0), while the live Studio reports 0.64.1, so the contract has moved under them and they still work. A client should be tested against the deployed version anyway.

**What carries over to Pothole Portolan.** The pattern fits the existing funnel workflow: a protected script in `scripts/blygger_client/` that creates a draft, publishes it with a changelog note, files it into a hopper, and writes the item id, version and `content_hash` to a publish log in git, so git still records exactly what went public. The main difference is that Pothole Portolan's prose is written by Claude in the funnel, with numbers filled from the claim ledger, rather than by the Studio's model inside `[TK]` scopes. Whether the Studio lets a client record its own `generated` provenance for text it wrote outside a `generate` call is not yet verified; `docs/api.md` mentions "client-recorded generation provenance" without saying more here.

## The Phase 2 draft path (built 2026-11-19)

The supervisor chose a Studio of its own (Option B of proposal `2026-11-19-002`). The client is in `scripts/blygger_client/`, written against the Studio repository's `openapi.json` (contract 5.14.0, Studio 0.65.0):

- `client.py` holds the owner API calls: capabilities, item reads, mention reads, draft create and edit, and a `publish()` that refuses unless `permissions.yaml` lists the current phase in `publish.agent_may_publish_in_phases`. Every list call passes a limit, edits carry `expectedRevision`, and the token appears only in the Authorization header, never in an error or a log line.
- `render.py` turns a queued draft into Markdown the Studio renders. The Studio uses markdown-it with raw HTML off, no footnote plugin and a `$...$` math plugin (`src/markdown.ts`, `src/math.ts`). Tables therefore render, footnotes become `[n]` with a numbered "Notes" section at the end, and dollar signs in prose are escaped while dollars inside URLs and code stay as written. The San Francisco draft was rendered with the Studio's own `renderMarkdown` on 2026-11-19: three tables, no formulas, and every source link intact.
- `push_drafts.py` is the Phase 2 job. For each city's highest-numbered draft in `queue/drafts/`, it creates a private Studio draft or edits the existing item's working copy, and records the item id, revision and the sha256 of the Markdown sent in `ledger/blyg_items.yaml`. It never publishes. It refuses to run when `PAUSE` exists, below Phase 2, or when the ledger names a different Studio.
- `.github/workflows/blyg-drafts.yml` runs it after each funnel run, on a supervisor push to `queue/drafts/`, and by hand (dry run by default). It runs no model, so the funnel's agent never holds the Studio token.

**How a dossier goes public in Phase 2.** The funnel queues a draft, and the workflow puts it in the Studio as a private draft. The supervisor reads it in the Studio and presses Publish there. A later version of the same dossier edits that item's working copy, so the supervisor's next publish becomes version 2 of the same item, with a changelog note.

**Not decided here.** `[TK]` provenance does not fit: the Studio's client-recorded provenance names blyg items as sources, and this project's sources are government documents. AI authorship is disclosed in the dossier text instead, as the template does now.

## The Pothole Portolan Studio (set up 2026-11-23)

The Studio lives in its own repository, `patwater/pothole-portolan-blyg`, which plays the role of World Machines' private `wm-blyg` checkout. It holds an unmodified Blygger Studio at release v0.71.0 with its full upstream history, so later upgrades merge cleanly, plus three Pothole Portolan files: `wrangler.jsonc` (Worker, D1 database and R2 bucket all named `pothole-portolan-blyg`, with no IDs), `scripts/pothole-deploy.sh` (the deploy command) and `POTHOLE-PORTOLAN.md` (the setup, connection and upgrade steps). Follow that file to deploy it from the Cloudflare dashboard and to connect this repository's `blyg-drafts` workflow.

Why no IDs: Wrangler 4.156 provisions a D1 database or R2 bucket named in the config when it does not exist, and finds it by name when it does, without prompting in a build. So the Cloudflare dashboard's Git build can deploy the Studio without anyone copying a database ID into the config, and no Cloudflare token had to be given to an agent.

**Verified on 2026-11-23, locally with `wrangler dev`:** the Worker builds (1.7 MB compressed), all 58 migrations apply, and `scripts/blygger_client/push_drafts.py` created the San Francisco draft, sent nothing on an unchanged rerun, and updated the same item for a version 2. A token with `owner:read` and `owner:draft` got HTTP 403 "insufficient scope" when it tried to publish. After an owner publish, the public item had its three tables, the numbered notes and no formulas. The Studio reported contract 6.3.0; the client's calls are unchanged from 5.14.0, since 6.0.0 removed only `getChanges`, which the client does not use.

**Not yet verified:** the Cloudflare dashboard build itself, including whether its default build token may create D1 and R2 resources. `POTHOLE-PORTOLAN.md` says what to do if it may not.

## Token renewal

Every Studio credential ends within 30 days, and this is deliberate in the Studio's design: manual tokens last 30 days with no refresh, and an OAuth grant ends 30 days after the owner's sign-in session began, however often it refreshes (`src/oauth.ts`, `docs/client-access.md` in the Studio). No setting lengthens it. Two alternatives were considered and not adopted. Patching the Studio's auth code would weaken a boundary its maintainers guard with security tests, and would conflict on upgrades. Giving the workflow the owner password so it could mint its own tokens would put the root credential in CI, which the Studio's design forbids.

Renewal is therefore made predictable instead. The tokens are JWTs, so `push_drafts.py` reads the expiry date from the token's payload (without logging the token) on every run. From 7 days before expiry it prints a warning, and `blyg-drafts.yml` opens one GitHub issue labelled `studio-token` with the four renewal steps. The workflow also runs weekly on Wednesdays, so the check happens even when no draft is waiting.

**A token works only at the address where it was created.** The Studio writes the address you were signed in at into the token, and checks it on every request (`src/oauth.ts`, `verifyBearer`). The first token was created at the temporary `workers.dev` address, so it was refused with HTTP 401 once `BLYGGER_API_BASE` moved to `https://potholeportolan.org/api` (2026-11-24). Create tokens while signed in at `https://potholeportolan.org/studio`.
