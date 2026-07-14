# ASU Seat Monitor

Watches one ASU class section for open seats and pushes an urgent alert to
your phone via [ntfy.sh](https://ntfy.sh) when a seat opens up. It only
notifies — enrollment is still manual, through My ASU.

## How it works

There's no public, unauthenticated ASU API for this. The real class search at
`catalog.apps.asu.edu` loads all its data through a JSON API gated behind an
ASU-login-derived token, so this monitor deliberately avoids that path
entirely (per design constraint: nothing here ever touches ASU credentials,
2FA, or the enrollment flow). Instead, `check_seats.py` runs a headless
Chromium browser (Playwright) against the public search page — exactly what
any anonymous visitor sees — and reads the seat count off the rendered
results.

Consecutive-failure tracking and the weekly check/failure counts are derived
from this repo's own GitHub Actions run history via the REST API, not a
hand-rolled state file. (The original design called for `actions/cache`, but
its keys are immutable — persisting an evolving counter needs extra
delete-and-recreate machinery. GitHub already records every run's pass/fail,
so reading that back is simpler and needs nothing beyond the token every
workflow already has.)

## One-time setup

1. **Create the secret and variables** — repo Settings → Secrets and
   variables → Actions:
   - Secret `NTFY_TOPIC` — your ntfy.sh topic name (keep it private-ish;
     anyone who knows the exact topic name can read anything sent to it).
   - Variable `CLASS_NUMBER` — the 5-digit class number, e.g. `71269`.
   - Variable `TERM_CODE` — ASU's term code, e.g. `2267` for Fall 2026.
   - Variable `MONITOR_ENABLED` — set to `true` to let `seat-check.yml` run.
     This is the kill switch: set it to anything else (or delete it) to stop
     the monitor once you've enrolled.

2. **Enable Actions** — repo Settings → Actions → General → allow workflows
   to run, if not already on by default.

3. **Push this repo** to GitHub (public repos get unlimited free Actions
   minutes, which is why nothing here is hardcoded — everything personal
   lives in secrets/variables).

4. **Run the test dispatch** — Actions tab → "Seat Check" → Run workflow →
   set `test` to `true`. Confirm the notification arrives on your phone
   before relying on the schedule.

5. **Run the heartbeat once manually** — Actions tab → "Weekly Heartbeat" →
   Run workflow, so you know what Monday mornings will actually look like.

## Reading the logs

Actions tab → pick a run → expand the "Run seat checks" or "Send weekly
heartbeat" step. Each check logs the parsed course/seat count or the error
that made it retry.

## Shutting it down

Once you've enrolled, set the `MONITOR_ENABLED` repo variable to `false` (or
delete it). `seat-check.yml`'s job is skipped entirely when it isn't exactly
`"true"` — no runs, no minutes used. The weekly heartbeat keeps running
regardless (it's cheap and doubles as the repo's keepalive), but you can
disable that workflow too from the Actions tab if you're fully done.

## "Silence means dead"

The weekly heartbeat is designed to *always* send something every Monday —
a calm status message if healthy, a high-priority alert if its own live
check failed. If a Monday ever passes with **no message at all**, that means
GitHub disabled or broke the workflows entirely (most likely the 60-day
inactivity auto-disable, though the heartbeat's own keepalive commit exists
specifically to prevent that). Treat total silence as a signal to go check
the Actions tab directly.

## Notes

- Cron scheduling is UTC and GitHub does not adjust for Daylight Saving Time
  — see the comment in `weekly-heartbeat.yml` for the exact drift.
- The scrape depends on `catalog.apps.asu.edu`'s current page structure
  (specifically the `#term`, `#classNbr`, `#search-button`, and
  `.class-accordion` selectors in `check_seats.py`). If ASU redesigns the
  page, the monitor will start failing every check, which the 5-consecutive-
  failures alert is there to catch.
