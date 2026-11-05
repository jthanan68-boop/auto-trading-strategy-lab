# StratBot on GitHub Actions (24/7, no card, no server)

One paper-trading cycle per trigger, running on GitHub's free runners. No VPS, no
credit card, no device left on. This is the cron route chosen over Termux/APK.

**Status: set up but not yet working.** The first run failed because the code on
GitHub is behind the code on the PC — see [Blocked](#blocked) below. The schedule
is currently `disabled_manually` so it does not spam a failing run every 5 minutes.

This file is the project's living status list. Per `AGENTS.md`, every change
updates it in the same commit.

## Files

| File | What it is |
| --- | --- |
| `.github/workflows/stratbot-cron.yml` | The whole thing: schedule, restore, one cycle, save |
| `deploy/GITHUB_ACTIONS.md` | This document |
| `state` branch (created by the workflow) | Holds only `data/crypto.sqlite3` — the ledger |

## How it works

1. GitHub triggers the workflow every 5 minutes (`*/5 * * * *`), or on demand.
2. The runner checks out `master`, installs `requirements.txt` with Python 3.12.
3. **Restore:** `data/crypto.sqlite3` is read out of the `state` branch. First run
   finds nothing and starts a fresh ledger.
4. **Run:** exactly the command from `start_crypto.bat`, minus the server:

       python run_bot.py --cycles 1 --refresh --no-backup \
         --db data/crypto.sqlite3 --symbols BTC,ETH \
         --style intraday --interval 5m \
         --portfolio --portfolio-cadence weekly

5. **Save:** the ledger is written back to the `state` branch as a single orphan
   commit, force-pushed over the previous one. History stays at one commit, so
   the repo does not grow by ~288 commits a day.

Two guards that matter:

- `concurrency: stratbot-cycle` — a delayed trigger cannot overlap a running
  one. Two cycles on one ledger would double-fill.
- `if: always()` on the save step — a crashed cycle still persists what it wrote
  before dying, instead of losing the partial cycle.

## Cost and limits

- The repo is **public**, which is what makes this free: standard-runner minutes
  are unlimited for public repos. On a private repo the free allowance is 2,000
  minutes/month and this schedule needs ~8,640 (each job bills as a whole
  minute), so it would run out in about a week.
- Scheduled triggers are **best effort**. 5–20 minute delays are normal under
  load and a trigger can be skipped. With 5-minute bars that means a missed
  decision, not a corrupt book.
- Cold cache every run: `data/cache/` is gitignored, so each run re-downloads
  12 days of 5m bars for BTC and ETH (~40–60 s of the job).
- **No dashboard.** The workflow is headless — nothing serves the FastAPI app.
  To look at results, fetch the ledger and serve it locally (see below).

## Running it by hand

    gh workflow run stratbot-cron.yml --ref master
    gh run list --workflow=stratbot-cron.yml --limit 5
    gh run watch <run-id> --exit-status
    gh run view <run-id> --log-failed

## Looking at the ledger

    git fetch origin state
    git show origin/state:data/crypto.sqlite3 > data/crypto.sqlite3
    python run_bot.py --serve --db data/crypto.sqlite3 --symbols BTC,ETH \
      --style intraday --interval 5m

Then open http://127.0.0.1:8000/ — the same dashboard, same API, on PC and phone.

## Changing what it trades

Everything lives in one place, the `env:`/run step of the workflow:

- **Symbols** — `--symbols BTC,ETH` (must be names `stratlab/data.py` knows, or
  `run_bot.py` exits with the list of valid ones).
- **Bar interval** — `--interval 5m` also wants the cron changed to match:
  `*/5` for 5m, `*/15` for 15m, `0 * * * *` for 60m.
- **Cadence** — `--portfolio-cadence weekly` (`session_close` / `weekly` /
  `monthly`).
- **Section** — for equities instead of crypto, change `DB`, the symbols, and
  restrict the cron to the NSE session (e.g. `*/5 3-10 * * 1-5` UTC).

## Blocked

The first run (`41966356365`) failed in 20 s with:

    run_bot.py: error: unrecognized arguments: --no-backup --portfolio --portfolio-cadence weekly

That is not a workflow bug. `origin/master` is **4 commits behind the working
tree**, and the pushed `run_bot.py` predates `--no-backup` and portfolio mode
(`stratbot/portfolio.py` is still untracked). The cron faithfully runs whatever
is on GitHub — today that is an older bot. Nothing else in the pipeline has been
proven yet, because it never reached the engine.

## Status & TODO

Legend: `[ ]` outstanding, `[x]` done, **Blocked** means nothing downstream can
be verified until it clears. Update this list in the same commit as the change.

**Done**

- [x] Repo made public (the free-tier requirement for unlimited runner minutes).
- [x] Workflow written, committed and pushed (`12c54e65`).
- [x] First manual run triggered and diagnosed (see [Blocked](#blocked)).
- [x] Schedule disabled while blocked.

**Blocked**

- [ ] **Commit and push the current working tree.** Until `run_bot.py`,
      `stratbot/portfolio.py` and the rest of the modified files are on
      `master`, the cron runs stale code and fails on the portfolio flags.

**Then**

- [ ] Re-enable the schedule (`gh workflow enable stratbot-cron.yml`).
- [ ] Re-run the workflow and confirm it goes green end to end.
- [ ] Confirm the `state` branch exists and carries `data/crypto.sqlite3`.
- [ ] Confirm a second run picks the ledger up (equity rows grow, no reset).
- [ ] Confirm the cycle leaves decisions in the run log.

**Decisions**

- [ ] Decide which host owns the crypto book — the local `start_crypto.bat` is
      running right now against the same filename, so the two ledgers diverge.
      One owner per section, or rename one DB.
- [ ] Rotate `STRATBOT_CONTROL_TOKEN`: `start_with_token.bat` hardcodes
      `local-3456721987351200` and the repo is now public. Harmless while
      nothing serves HTTP, dangerous the day `--serve` is exposed.

**Optional**

- [ ] Persist `data/cache/` between runs with `actions/cache` to cut ~40 s off
      every job.
- [ ] A dashboard for phone/PC without a PC on — GitHub Pages reading a JSON
      snapshot committed alongside the ledger.
- [ ] A second job for the NSE equities book, session-hours cron only.

