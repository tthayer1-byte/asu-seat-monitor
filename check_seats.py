#!/usr/bin/env python3
"""ASU class seat monitor.

Scrapes ASU's public class search (catalog.apps.asu.edu) with a headless
browser -- exactly what an anonymous visitor sees, no login involved -- and
alerts an ntfy.sh topic when seats open up. Never touches ASU credentials,
tokens, or the enrollment flow; it only reads the public search results page.

Failure/recovery tracking and the weekly heartbeat counts are derived from
this repo's own GitHub Actions run history via the REST API, rather than a
hand-rolled state file. That avoids the immutable-key headaches of
actions/cache (each save needs a new key, so "just overwrite the counter"
doesn't work without extra cache-eviction machinery) and needs nothing beyond
the GITHUB_TOKEN every workflow already gets.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CATALOG_URL = "https://catalog.apps.asu.edu/catalog/classes"
MY_ASU_URL = "https://my.asu.edu"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
FAILURE_THRESHOLD = 5
SEAT_CHECK_WORKFLOW = "seat-check.yml"


def log(msg):
    print(f"[seat-monitor] {msg}", flush=True)


def send_ntfy(topic, title, body, priority="default", tags=None, click=None):
    url = f"https://ntfy.sh/{topic}"
    # HTTP header values must be Latin-1; the body has no such restriction,
    # so non-ASCII (e.g. an em dash) belongs in body text, never in a header.
    ascii_title = title.encode("ascii", errors="replace").decode("ascii")
    headers = {
        "Title": ascii_title,
        "Priority": priority,
        "Content-Type": "text/plain; charset=utf-8",
    }
    if tags:
        headers["Tags"] = tags
    if click:
        headers["Click"] = click
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    last_err = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            log(f"ntfy sent: {title!r} (priority={priority})")
            return
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            log(f"ntfy send attempt {attempt}/3 failed: {e}")
            if attempt < 3:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"failed to send ntfy notification after 3 attempts: {last_err}")


def fetch_seats(class_number, term_code, timeout_ms=45000):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(CATALOG_URL, wait_until="networkidle", timeout=timeout_ms)
            # The cookie-consent banner can overlay the search form; it isn't
            # needed for a read-only scrape, so drop it rather than click through it.
            page.evaluate(
                "() => { document.querySelectorAll('[id^=cassie]').forEach(e => e.remove()); "
                "document.querySelectorAll('[class*=cassie]').forEach(e => e.remove()); }"
            )
            page.click("text=Advanced Search", timeout=15000)
            page.select_option("#term", term_code, timeout=15000)
            page.fill("#classNbr", class_number, timeout=15000)
            page.click("#search-button", timeout=15000)
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            page.wait_for_timeout(500)

            rows = page.query_selector_all(".class-accordion")
            if not rows:
                if "No classes found" in page.inner_text("body"):
                    raise RuntimeError(
                        f"no class found for class number {class_number} in term {term_code} "
                        "(double check CLASS_NUMBER/TERM_CODE repo variables are still correct)"
                    )
                raise RuntimeError("search results did not render as expected")

            text = rows[0].inner_text()
            seats_m = re.search(r"Open seats:\s*\n?\s*(\d+)\s*of\s*(\d+)", text)
            if not seats_m:
                raise RuntimeError(f"could not parse seat count from result row: {text[:200]!r}")

            course_m = re.search(r"Course:\s*\n(.+)", text)
            course = course_m.group(1).strip() if course_m else "Class"

            return {
                "open": int(seats_m.group(1)),
                "total": int(seats_m.group(2)),
                "course": course,
            }
        finally:
            browser.close()


def fetch_seats_with_retry(class_number, term_code, attempts=3):
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            return fetch_seats(class_number, term_code)
        except Exception as e:  # noqa: BLE001 - want to retry on anything transient
            last_err = e
            log(f"attempt {attempt}/{attempts} failed: {e}")
            if attempt < attempts:
                time.sleep(2 ** attempt)
    raise last_err


def github_api_get(path, token):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "asu-seat-monitor",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def recent_run_conclusions(repo, token, per_page=15):
    """Conclusions of the most recent completed seat-check runs, newest first."""
    path = f"/repos/{repo}/actions/workflows/{SEAT_CHECK_WORKFLOW}/runs?per_page={per_page}&status=completed"
    data = github_api_get(path, token)
    return [r["conclusion"] for r in data.get("workflow_runs", [])]


def run_counts_since(repo, token, since_iso):
    """(total completed runs, failed runs) of seat-check since an ISO timestamp.

    Uses the API's total_count so it doesn't need to paginate through
    potentially thousands of runs from a week of 5-minute-interval checks.
    """
    created = urllib.parse.quote(f">={since_iso}", safe="")
    base = f"/repos/{repo}/actions/workflows/{SEAT_CHECK_WORKFLOW}/runs?status=completed&created={created}&per_page=1"
    total = github_api_get(base, token)["total_count"]
    failed = github_api_get(base + "&conclusion=failure", token)["total_count"]
    return total, failed


def cmd_check(args):
    try:
        result = fetch_seats_with_retry(args.class_number, args.term_code)
    except Exception as e:  # noqa: BLE001
        log(f"ERROR: all attempts failed: {e}")
        sys.exit(1)

    log(f"{result['course']} (#{args.class_number}): {result['open']} of {result['total']} seats open")

    if result["open"] > 0:
        send_ntfy(
            args.ntfy_topic,
            title="SEAT OPEN - enroll NOW",
            body=(
                f"{result['course']} (#{args.class_number}): "
                f"{result['open']} of {result['total']} seats open.\n"
                f"Enroll now: {MY_ASU_URL}"
            ),
            priority="urgent",
            tags="rotating_light",
            click=MY_ASU_URL,
        )
    sys.exit(0)


def cmd_test_notify(args):
    send_ntfy(
        args.ntfy_topic,
        title="Test notification",
        body="If you see this, the ASU seat monitor pipeline is wired up correctly.",
        priority="default",
        tags="white_check_mark",
    )


def cmd_monitor_health(args):
    conclusions = recent_run_conclusions(args.repo, args.github_token)
    consecutive_prior_failures = 0
    for c in conclusions:
        if c == "failure":
            consecutive_prior_failures += 1
        else:
            break

    if args.current_run_ok:
        if consecutive_prior_failures >= FAILURE_THRESHOLD:
            send_ntfy(
                args.ntfy_topic,
                title="Monitor recovered",
                body="The ASU seat monitor is back up and checking normally again.",
                priority="default",
                tags="white_check_mark",
            )
            log("sent recovery notice")
        else:
            log("monitor healthy, no alert needed")
    else:
        streak = consecutive_prior_failures + 1
        log(f"current run failed; consecutive failure streak = {streak}")
        if streak == FAILURE_THRESHOLD:
            send_ntfy(
                args.ntfy_topic,
                title="Seat monitor is broken",
                body=(
                    f"{FAILURE_THRESHOLD} checks in a row have failed. "
                    "Staying quiet until it recovers — check the Actions logs."
                ),
                priority="low",
                tags="warning",
            )
            log("sent monitor-down alert")
        elif streak > FAILURE_THRESHOLD:
            log("already alerted for this outage, staying quiet")
        else:
            log("below alert threshold, staying quiet")


def cmd_weekly_heartbeat(args):
    ok = True
    result = None
    try:
        result = fetch_seats_with_retry(args.class_number, args.term_code)
    except Exception as e:  # noqa: BLE001
        ok = False
        log(f"heartbeat live check failed: {e}")

    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        checks_run, failures = run_counts_since(args.repo, args.github_token, since)
    except Exception as e:  # noqa: BLE001
        log(f"could not fetch run history: {e}")
        checks_run, failures = "?", "?"

    if not ok:
        send_ntfy(
            args.ntfy_topic,
            title="Weekly check-in FAILED",
            body=(
                "The weekly live check could not reach ASU's class search. "
                "The monitor may have been failing silently this week — "
                "check the Actions logs."
            ),
            priority="high",
            tags="warning",
        )
        sys.exit(1)

    body = (
        f"\U0001F4CB Weekly check-in: monitor healthy.\n"
        f"{result['course']} (#{args.class_number}): {result['open']}/{result['total']} seats.\n"
        f"{checks_run} checks run this week, {failures} failures.\n"
        f"Next heartbeat Monday."
    )
    send_ntfy(
        args.ntfy_topic,
        title="Weekly monitor check-in",
        body=body,
        priority="default",
        tags="clipboard",
    )


def build_parser():
    p = argparse.ArgumentParser(description="ASU class seat monitor")
    p.add_argument("--class-number", default=os.environ.get("CLASS_NUMBER"))
    p.add_argument("--term-code", default=os.environ.get("TERM_CODE"))
    p.add_argument("--ntfy-topic", default=os.environ.get("NTFY_TOPIC"))
    p.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    p.add_argument("--github-token", default=os.environ.get("GITHUB_TOKEN"))

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("test-notify")

    mh = sub.add_parser("monitor-health")
    mh.add_argument("--current-run-ok", choices=["true", "false"], required=True)

    sub.add_parser("weekly-heartbeat")
    return p


def main():
    args = build_parser().parse_args()

    if not args.ntfy_topic:
        log("ERROR: NTFY_TOPIC is not set")
        sys.exit(1)

    if args.command in ("check", "weekly-heartbeat") and (not args.class_number or not args.term_code):
        log("ERROR: CLASS_NUMBER/TERM_CODE are not set")
        sys.exit(1)

    if args.command in ("monitor-health", "weekly-heartbeat") and (not args.repo or not args.github_token):
        log("ERROR: repo/github-token are required for this command")
        sys.exit(1)

    if args.command == "check":
        cmd_check(args)
    elif args.command == "test-notify":
        cmd_test_notify(args)
    elif args.command == "monitor-health":
        args.current_run_ok = args.current_run_ok == "true"
        cmd_monitor_health(args)
    elif args.command == "weekly-heartbeat":
        cmd_weekly_heartbeat(args)


if __name__ == "__main__":
    main()
