"""
One-command, unattended harvest supervisor.

Turn the VPN on, run this, walk away. It runs the phases in order, retries a
phase that dies, re-mints WAF cookies when they expire, and can be stopped and
restarted at any point without losing work.

    python scraper/run_all.py                 # resume whatever is incomplete
    python scraper/run_all.py --full-run      # WIPE everything, harvest from zero
    python scraper/run_all.py --full-run --retry-errors
                                              # resume an interrupted full run
    python scraper/run_all.py --new-trials    # frequent update (see below)
    python scraper/run_all.py --status        # just report progress, no network
    python scraper/run_all.py --gzip          # store documents compressed
    python scraper/run_all.py --workers 4

Modes:
  (default)      resume: fetch whatever is still missing; never wipes anything.
  --full-run     delete the database, all downloaded documents and the exports,
                 then harvest everything from scratch. If that run is stopped
                 before it finishes, restart it with --full-run --retry-errors:
                 that resumes WITHOUT wiping and retries the failed rows.
  --new-trials   re-scan every listing page (~1860), then download only trials
                 that are new (not recorded in the database yet) or whose list
                 row changed since they were recorded (e.g. status moved from
                 recruiting to completed). Exports are rebuilt at the end.

Everything is also appended to out/run.log, so the log can be shared afterwards
(with the VPN off) to diagnose anything that went wrong.
"""
import argparse
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

from common import ROOT, DB_FILE, DOCS_DIR, HTML_DIR, db

LOG = ROOT / "out" / "run.log"
PY = [sys.executable, "-X", "utf8"]


def log(msg):
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def wipe(assume_yes):
    """Delete every byte of harvested data so a full run starts from zero."""
    targets = [DB_FILE, Path(str(DB_FILE) + "-wal"), Path(str(DB_FILE) + "-shm"),
               DOCS_DIR, HTML_DIR, ROOT / "out" / "export"]
    if not assume_yes:
        if not sys.stdin.isatty():
            log("!!! --full-run deletes all harvested data; "
                "re-run with --yes to confirm non-interactively")
            sys.exit(1)
        print("\nWARNING: --full-run will DELETE all previously harvested data:")
        for t in targets:
            print(f"  - {t}")
        if input("\ntype 'yes' to proceed: ").strip().lower() != "yes":
            print("aborted - nothing was deleted")
            sys.exit(1)
    log("wiping previous harvest: database, documents, exports")
    for t in targets:
        if t.is_dir():
            shutil.rmtree(t, ignore_errors=True)
        elif t.exists():
            t.unlink()


def run(script, *args, tries=3):
    """Run a phase, retrying if it exits non-zero."""
    for attempt in range(1, tries + 1):
        cmd = PY + [str(ROOT / "scraper" / script), *map(str, args)]
        log(f"--> {script} {' '.join(map(str, args))} (attempt {attempt}/{tries})")
        t0 = time.time()
        p = subprocess.run(cmd, cwd=str(ROOT))
        el = time.time() - t0
        if p.returncode == 0:
            log(f"<-- {script} finished in {el/60:.1f} min")
            return True
        if p.returncode in (130, -2, 3221225786):      # Ctrl-C
            log(f"<-- {script} interrupted by user")
            return False
        log(f"!!! {script} exited {p.returncode} after {el/60:.1f} min")
        if attempt < tries:
            time.sleep(30 * attempt)
    return False


def status():
    con = db()
    def one(q, d=0):
        r = con.execute(q).fetchone()
        return (r[0] if r and r[0] is not None else d)

    pages_ok = one("SELECT COUNT(*) FROM pages WHERE error IS NULL")
    pages_err = one("SELECT COUNT(*) FROM pages WHERE error IS NOT NULL")
    trials = one("SELECT COUNT(*) FROM trials")
    docs = one("SELECT COUNT(*) FROM trials WHERE doc_path IS NOT NULL")
    parsed = one("SELECT COUNT(*) FROM trials WHERE parsed_at IS NOT NULL")
    errs = one("SELECT COUNT(*) FROM trials WHERE error IS NOT NULL")
    changed = one("SELECT COUNT(*) FROM trials WHERE needs_refetch=1")
    nbytes = one("SELECT SUM(doc_bytes) FROM trials")

    print("\n" + "=" * 58)
    print("  harvest status")
    print("=" * 58)
    print(f"  index pages done     {pages_ok} / 1860" + (f"  ({pages_err} failed)" if pages_err else ""))
    print(f"  trials discovered    {trials} / 37197")
    print(f"  documents saved      {docs}" + (f" / {trials}" if trials else ""))
    print(f"  documents parsed     {parsed}")
    print(f"  rows with errors     {errs}")
    if changed:
        print(f"  changed, to refetch  {changed}")
    print(f"  bytes on disk        {(nbytes or 0)/1e9:.2f} GB")
    if trials and docs < trials:
        print(f"\n  -> {trials - docs} documents still to fetch")
    print("=" * 58 + "\n")
    return trials, docs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="report progress and exit")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--full-run", action="store_true",
                      help="wipe all harvested data and collect everything from "
                           "scratch (with --retry-errors: resume an interrupted "
                           "full run instead - no wipe)")
    mode.add_argument("--new-trials", action="store_true",
                      help="re-scan the whole listing and collect only trials "
                           "that are new or whose list row changed")
    ap.add_argument("--retry-errors", action="store_true",
                    help="re-attempt failed rows from the first round "
                         "(with --full-run: resume the run, do NOT wipe)")
    ap.add_argument("--yes", action="store_true",
                    help="don't ask before wiping (--full-run)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--gzip", action="store_true")
    ap.add_argument("--skip-index", action="store_true")
    ap.add_argument("--skip-details", action="store_true")
    ap.add_argument("--rounds", type=int, default=3,
                    help="passes over the pending set, to mop up failures")
    a = ap.parse_args()

    if a.status:
        status()
        return 0

    wiping = a.full_run and not a.retry_errors
    if wiping:
        wipe(a.yes)

    tag = ("full run from scratch" if wiping else
           "full run (resuming, no wipe)" if a.full_run else
           "new-trials update" if a.new_trials else
           "harvest run (resuming)")
    log("=" * 60)
    log(f"harvest run starting - {tag}")
    log("=" * 60)

    # Phase 0 - prove the parsers still work before touching the network
    if not run("selftest.py", tries=1):
        log("!!! self-test failed - fix that before harvesting")
        return 1

    # Phase 1 - mint WAF cookies
    if not run("bootstrap.py", "--out", ROOT / "out" / "session.json", tries=3):
        log("!!! could not clear the WAF. Is the VPN (Chinese exit) up?")
        return 1

    # Phase 2 - the index
    if not a.skip_index:
        con = db()
        (n0,) = con.execute("SELECT COUNT(*) FROM trials").fetchone()
        for rnd in range(1, a.rounds + 1):
            log(f"### index round {rnd}/{a.rounds}")
            idx_args = ["--workers", a.workers]
            if a.new_trials and rnd == 1:
                # update mode: re-fetch every page so new/changed rows show up
                idx_args.append("--refresh")
            run("harvest_index.py", *idx_args, tries=2)
            con = db()
            (left,) = con.execute(
                "SELECT COUNT(*) FROM pages WHERE error IS NOT NULL").fetchone()
            (got,) = con.execute(
                "SELECT COUNT(*) FROM pages WHERE error IS NULL").fetchone()
            if not left:
                log(f"index complete ({got} pages)")
                break
        if a.new_trials:
            con = db()
            (n1,) = con.execute("SELECT COUNT(*) FROM trials").fetchone()
            (chg,) = con.execute(
                "SELECT COUNT(*) FROM trials WHERE needs_refetch=1").fetchone()
            log(f"listing scan: {n1 - n0} new trials discovered, "
                f"{chg} known trials changed")

    # Phase 3 - the documents
    if not a.skip_details:
        args = ["--workers", a.workers]
        if a.gzip:
            args.append("--gzip")
        if a.new_trials:
            # include trials flagged because their list row changed
            args.append("--refetch")
        for rnd in range(1, a.rounds + 1):
            log(f"### details round {rnd}/{a.rounds}")
            extra = list(args)
            if a.retry_errors or rnd > 1:
                extra.append("--retry-errors")
            run("harvest_details.py", *extra, tries=2)
            con = db()
            (left,) = con.execute(
                "SELECT COUNT(*) FROM trials WHERE doc_path IS NULL "
                "OR needs_refetch=1").fetchone()
            log(f"{left} documents still pending")
            if not left:
                log("all documents fetched")
                break

    # Phase 4 - exports
    run("export.py", tries=2)

    trials, docs = status()
    log(f"run finished: {docs}/{trials} documents")
    log(f"log file: {LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
