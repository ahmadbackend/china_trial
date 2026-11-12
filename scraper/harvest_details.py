"""
Phase 2: for every trial GUID in out/trials.db, POST to
clinicaltrials.searchlistdetail.dhtml?_export=doc, save the WordML file and
store its parsed fields.

That single request yields BOTH the downloadable document and the full detail
record, so there is no need to scrape the HTML detail page as well.

Resumable: a trial is skipped once it has a doc on disk with a recorded size.
Interrupt with Ctrl-C at any time and re-run the same command.

Usage:
  python scraper/harvest_details.py                  # everything pending
  python scraper/harvest_details.py --workers 4
  python scraper/harvest_details.py --limit 25       # smoke test
  python scraper/harvest_details.py --gzip           # store .doc.gz (~10x smaller)
  python scraper/harvest_details.py --retry-errors   # re-attempt failures
"""
import argparse
import gzip
import hashlib
import queue
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

from common import DOCS_DIR, DOC_URL, Session, db
from parse_doc import parse_wordml

_tls = threading.local()
STOP = threading.Event()


def sess():
    if not hasattr(_tls, "s"):
        _tls.s = Session()
    return _tls.s


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def shard_dir(ctr_or_id):
    """Spread 37k files over 2-char buckets so directory listings stay usable."""
    key = (ctr_or_id or "zz")[-2:]
    return DOCS_DIR / key


def fetch_one(task, do_parse=True, use_gzip=False):
    tid, ctr = task
    if STOP.is_set():
        return None
    try:
        r = sess().post(DOC_URL, {"id": tid}, expect="doc")
        body = r.content
        name = (ctr or tid) + ".doc" + (".gz" if use_gzip else "")
        d = shard_dir(ctr or tid)
        d.mkdir(parents=True, exist_ok=True)
        path = d / name
        tmp = path.with_suffix(path.suffix + ".part")
        if use_gzip:
            with gzip.open(tmp, "wb", compresslevel=6) as fh:
                fh.write(body)
        else:
            tmp.write_bytes(body)
        tmp.replace(path)

        rec = {"id": tid,
               "doc_path": str(path.relative_to(DOCS_DIR.parent)).replace("\\", "/"),
               "doc_bytes": len(body),
               "doc_sha256": hashlib.sha256(body).hexdigest(),
               "fetched_at": now(), "error": None,
               "parsed_at": None, "fields_json": None, "rows_json": None,
               "ctr_no": ctr}
        if do_parse:
            import json as _json
            p = parse_wordml(body)
            rec["fields_json"] = _json.dumps(p["fields"], ensure_ascii=False)
            rec["rows_json"] = _json.dumps(p["sections"], ensure_ascii=False)
            rec["parsed_at"] = now()
            if p["ctr_no"]:
                rec["ctr_no"] = p["ctr_no"]
        return rec
    except Exception as e:
        return {"id": tid, "error": f"{type(e).__name__}: {e}"[:400],
                "fetched_at": now(), "doc_path": None, "doc_bytes": None,
                "doc_sha256": None, "parsed_at": None,
                "fields_json": None, "rows_json": None, "ctr_no": ctr}


UPSERT = """
UPDATE trials SET doc_path=:doc_path, doc_bytes=:doc_bytes, doc_sha256=:doc_sha256,
  fetched_at=:fetched_at, parsed_at=:parsed_at, fields_json=:fields_json,
  rows_json=:rows_json, error=:error, ctr_no=COALESCE(:ctr_no, ctr_no)
WHERE id=:id
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--gzip", action="store_true", help="store .doc.gz")
    ap.add_argument("--no-parse", action="store_true", help="download only")
    ap.add_argument("--retry-errors", action="store_true")
    a = ap.parse_args()

    con = db()
    where = "doc_path IS NULL OR doc_bytes IS NULL"
    if a.retry_errors:
        where += " OR error IS NOT NULL"
    sql = f"SELECT id, ctr_no FROM trials WHERE {where} ORDER BY ctr_no DESC"
    if a.limit:
        sql += f" LIMIT {a.limit}"
    todo = con.execute(sql).fetchall()
    (total,) = con.execute("SELECT COUNT(*) FROM trials").fetchone()
    (have,) = con.execute(
        "SELECT COUNT(*) FROM trials WHERE doc_path IS NOT NULL").fetchone()

    if not total:
        print("[!] no trials in db - run harvest_index.py first", file=sys.stderr)
        return 1
    print(f"[+] {total} trials known, {have} already downloaded, {len(todo)} to do")
    if not todo:
        print("[=] nothing pending")
        return 0

    def on_sigint(*_):
        if not STOP.is_set():
            print("\n[~] stopping after in-flight requests ... (Ctrl-C again to kill)",
                  flush=True)
            STOP.set()
        else:
            sys.exit(130)
    signal.signal(signal.SIGINT, on_sigint)

    # producer/consumer: workers fetch, the main thread owns the sqlite handle
    work = queue.Queue()
    for t in todo:
        work.put(t)
    results = queue.Queue(maxsize=256)

    def worker():
        while not STOP.is_set():
            try:
                task = work.get_nowait()
            except queue.Empty:
                return
            try:
                rec = fetch_one(task, not a.no_parse, a.gzip)
                if rec is not None:
                    results.put(rec)
            finally:
                work.task_done()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(a.workers)]
    for t in threads:
        t.start()

    t0 = time.time()
    n_ok = n_err = n_bytes = 0
    pending = []
    done = 0
    try:
        while done < len(todo):
            if STOP.is_set() and results.empty() and not any(t.is_alive() for t in threads):
                break
            try:
                rec = results.get(timeout=1.0)
            except queue.Empty:
                if not any(t.is_alive() for t in threads) and results.empty():
                    break
                continue
            done += 1
            pending.append(rec)
            if rec.get("error"):
                n_err += 1
                print(f"[!] {rec.get('ctr_no') or rec['id']}: {rec['error']}", flush=True)
            else:
                n_ok += 1
                n_bytes += rec["doc_bytes"] or 0
            if len(pending) >= 25:
                con.executemany(UPSERT, pending)
                pending.clear()
            if done % 25 == 0 or done == len(todo):
                el = time.time() - t0
                rate = done / el if el else 0
                eta = (len(todo) - done) / rate if rate else 0
                print(f"[{done}/{len(todo)}] ok={n_ok} err={n_err} "
                      f"{n_bytes/1e6:.0f} MB | {rate*60:.0f} docs/min | "
                      f"ETA {eta/3600:.1f} h", flush=True)
    finally:
        if pending:
            con.executemany(UPSERT, pending)
        STOP.set()

    el = time.time() - t0
    print(f"\n[=] fetched {n_ok} docs ({n_bytes/1e6:.0f} MB) in {el/60:.1f} min, {n_err} errors")
    (left,) = con.execute(
        "SELECT COUNT(*) FROM trials WHERE doc_path IS NULL").fetchone()
    if left:
        print(f"[i] {left} still pending - re-run this command to continue")
    if n_ok:
        print(f"[i] avg {n_bytes/n_ok/1024:.0f} KB/doc; "
              f"projected total {(n_bytes/n_ok)*total/1e9:.1f} GB for {total} docs"
              + (" (gzipped)" if a.gzip else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
