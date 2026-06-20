# SEO Monitor

Automated, continuous SEO / availability monitoring for a configurable list of
sites. It runs on a schedule via GitHub Actions, and **the commit history is the
time-series log** — each run is committed so you can correlate a ranking drop
with whatever else changed at that moment (downtime, a 403 to Googlebot, content
duplication, a latency spike, etc.).

The current focus is two sites that keep bouncing on and off page 1 of Google:
`totalsportek.tech` and `totalsportek.bio`. The two leading hypotheses it tests:

1. **Googlebot is being intermittently blocked** (403/5xx) while real browsers
   get 200 — this would explain the bouncing. Detected as `CRAWLER_BLOCKED`.
2. **The `.tech` and `.bio` sites serve duplicate content** and compete with
   each other, making Google flip-flop between them. Detected as
   `DUPLICATE_CONTENT`.

<!-- STATUS:BEGIN -->
_No runs recorded yet. Run `python scripts/run_all.py --dry-run` for a sample
report, or see the live dashboard at [`STATUS.md` on the **`data`** branch](../../blob/data/STATUS.md)
once the scheduled workflow has run._
<!-- STATUS:END -->

## How it works

```
config.yaml ──► run_all.py ──► monitor_response ─┐
                                compare_duplicates ├─► analyze ──► report ──► data branch
                                                  ─┘                          + README status
```

- **monitor_response** — for each site × each user-agent (Googlebot desktop,
  Googlebot mobile, real browser), fetches the homepage and key paths and
  records HTTP status, response time, TTFB, redirect chain, final URL, content
  length and a SHA-256 of the body. Flags `CRAWLER_BLOCKED` when Googlebot is
  treated worse than a browser, and `MOBILE_FETCH_FAIL` when mobile Googlebot
  fails but desktop succeeds.
- **compare_duplicates** — fetches matching pages from both domains, strips
  nav/script/style to visible text, and computes similarity (difflib
  `SequenceMatcher` ratio + token-based Jaccard on word shingles). Flags
  `DUPLICATE_CONTENT` (critical) when the overall average exceeds the threshold,
  and `DUPLICATE_PAGE` (warning) when any single page is near-identical across
  the pair — catching one duplicated page the average would otherwise hide.
- **analyze** — diffs the current run against the previous run and emits
  `findings` (new 403s, status-code changes, latency spikes, content-hash
  changes on key pages, plus the flags above).
- **report** — writes a timestamped JSON + a human-readable markdown report and
  updates the status block + badges in this README.

## The `data` branch design

Committing results every hour would bloat the code history, so **results never
land on `main`**. Code lives on `main`; the JSON/markdown results are committed
to a separate **`data`** branch by the GitHub Actions workflow. Locally, the
`data/` directory is git-ignored on `main`.

Each run on the `data` branch writes:

- `run-<stamp>.json` — full machine-readable run record (the time-series log);
- `report-<stamp>.md` — human-readable report for that run;
- `latest-summary.json` — small pointer the workflow reads to build the commit
  message and decide on alert Issues;
- `STATUS.md` — a live status dashboard (badges + per-site table).

The data directory location is overridable with the `SEO_MONITOR_DATA_DIR`
environment variable; the workflow sets it to a `data`-branch worktree so the
same code reads previous runs and writes new ones without checking out `main`'s
`data/`. A local `python scripts/run_all.py` (no env var) writes to `./data/`
and updates the status block in this README for at-a-glance local visibility.

## Run locally

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Unix:     source .venv/bin/activate
pip install -r requirements.txt

# Run all checks and print the report WITHOUT writing or committing anything:
python scripts/run_all.py --dry-run
```

Each script can also be run on its own (e.g. `python scripts/monitor_response.py`)
and will print its JSON to stdout — handy for debugging a single check.

## Configure

Everything tunable lives in [`config.yaml`](config.yaml): the list of sites and
key paths, target keywords, the cron schedule, alert thresholds (open issue on
403, duplicate-similarity threshold, SSL expiry warning days, latency-spike
multiplier), request timing, and the user-agents to test. **To add a site (e.g.
Footybite) just add an entry under `sites`** — no code changes needed. To
compare two domains for duplicate content, add the pair under
`duplicate_compare.pairs`.

## Automation & alerting

The workflow in [`.github/workflows/analyze.yml`](.github/workflows/analyze.yml):

- runs on the cron schedule in `config.yaml` (hourly by default) and on manual
  `workflow_dispatch`;
- installs deps, runs `python scripts/run_all.py`;
- commits the JSON + markdown results to the **`data`** branch with a message
  like `Analysis <UTC time> — tech: <status> | bio: <status>`;
- **opens a GitHub Issue** labelled `auto-alert` when a run produces any
  `critical` finding (e.g. `CRAWLER_BLOCKED`, a new 403), and **closes** it when
  the condition clears.

## Roadmap

Phase 1 (this) is the technical-diagnosis core. Later phases (separate work):
Search Console positions (P2), full on-page audit (P3), Core Web Vitals + SSL/DNS
+ a Pages dashboard (P4), a correlation engine + weekly digest + auto-PR
remediation (P5), and optional paid backlink/SERP APIs (P6).
