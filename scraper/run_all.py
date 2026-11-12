"""
One-command, unattended harvest supervisor.

Turn the VPN on, run this, walk away. It runs the phases in order, retries a
phase that dies, re-mints WAF cookies when they expire, and can be stopped and
restarted at any point without losing work.

    python scraper/run_all.py                 # the whole job
    python scraper/run_all.py --status        # just report progress, no network
    python scraper/run_all.py --gzip          # store documents compressed
    python scraper/run_all.py --skip-index    # details only
    python scraper/run_all.py --workers 4

Everything is also appended to out/run.log, so the log can be shared afterwards
(with the VPN off) to diagnose anything that went wrong.
"""
import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

from common import ROOT, db

LOG = ROOT / "out" / "run.log"
PY = [sys.executable, "-X", "utf8"]


def log(msg):
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


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
    nbytes = one("SELECT SUM(doc_bytes) FROM trials")

    print("\n" + "=" * 58)
    print("  harvest status")
    print("=" * 58)
    print(f"  index pages done     {pages_ok} / 1860" + (f"  ({pages_err} failed)" if pages_err else ""))
    print(f"  trials discovered    {trials} / 37197")
    print(f"  documents saved      {docs}" + (f" / {trials}" if trials else ""))
    print(f"  documents parsed     {parsed}")
    print(f"  rows with errors     {errs}")
    print(f"  bytes on disk        {(nbytes or 0)/1e9:.2f} GB")
    if trials and docs < trials:
        print(f"\n  -> {trials - docs} documents still to fetch")
    print("=" * 58 + "\n")
    return trials, docs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="report progress and exit")
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

    log("=" * 60)
    log("harvest run starting")
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
        for rnd in range(1, a.rounds + 1):
            log(f"### index round {rnd}/{a.rounds}")
            run("harvest_index.py", "--workers", a.workers, tries=2)
            con = db()
            (left,) = con.execute(
                "SELECT COUNT(*) FROM pages WHERE error IS NOT NULL").fetchone()
            (got,) = con.execute("SELECT COUNT(*) FROM pages WHERE error IS NULL").fetchone()
            if got >= 1860 and not left:
                log("index complete")
                break

    # Phase 3 - the documents
    if not a.skip_details:
        args = ["--workers", a.workers]
        if a.gzip:
            args.append("--gzip")
        for rnd in range(1, a.rounds + 1):
            log(f"### details round {rnd}/{a.rounds}")
            extra = args + (["--retry-errors"] if rnd > 1 else [])
            run("harvest_details.py", *extra, tries=2)
            con = db()
            (left,) = con.execute(
                "SELECT COUNT(*) FROM trials WHERE doc_path IS NULL").fetchone()
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
