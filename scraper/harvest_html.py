"""
Phase 2b: save each trial's detail page exactly as the site serves it, i.e. the
same bytes as out/export/example_detail_CTR20264276.html.

Stored verbatim - no rewriting, no pretty-printing - so the file is a faithful
record of the page. Files land in out/html/<nn>/CTR########.html, sharded on the
CTR suffix like the documents.

Works as a backfill: trials whose document was already fetched simply get their
HTML added, nothing is re-downloaded.

Usage:
  python scraper/harvest_html.py                    # everything pending
  python scraper/harvest_html.py --workers 4
  python scraper/harvest_html.py --limit 20         # smoke test
  python scraper/harvest_html.py --gzip             # store .html.gz (~3x smaller)
  python scraper/harvest_html.py --retry-errors
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

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

from common import DETAIL_URL, HTML_DIR, Session, db, search_form   # noqa: E402

_tls = threading.local()
STOP = threading.Event()


def sess():
    if not hasattr(_tls, "s"):
        _tls.s = Session()
    return _tls.s


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def shard_dir(key):
    return HTML_DIR / (key or "zz")[-2:]


def fetch_one(task, use_gzip=False):
    tid, ctr, row_index = task
    if STOP.is_set():
        return None
    try:
        # ckm_index is the row number the list page passes; any valid value
        # works, so fall back to 1 when the index did not record one.
        form = search_form(1, id=tid, ckm_index=row_index or 1)
        r = sess().post(DETAIL_URL, form, expect="detail")
        body = r.content                      # keep the exact bytes served
        d = shard_dir(ctr or tid)
        d.mkdir(parents=True, exist_ok=True)
        path = d / ((ctr or tid) + ".html" + (".gz" if use_gzip else ""))
        tmp = path.with_suffix(path.suffix + ".part")
        if use_gzip:
            with gzip.open(tmp, "wb", compresslevel=6) as fh:
                fh.write(body)
        else:
            tmp.write_bytes(body)
        tmp.replace(path)
        return {"id": tid,
                "html_path": str(path.relative_to(HTML_DIR.parent)).replace("\\", "/"),
                "html_bytes": len(body),
                "html_sha256": hashlib.sha256(body).hexdigest(),
                "html_fetched_at": now(), "html_error": None}
    except Exception as e:
        return {"id": tid, "html_path": None, "html_bytes": None,
                "html_sha256": None, "html_fetched_at": now(),
                "html_error": f"{type(e).__name__}: {e}"[:400]}


UPSERT = """
UPDATE trials SET html_path=:html_path, html_bytes=:html_bytes,
  html_sha256=:html_sha256, html_fetched_at=:html_fetched_at,
  html_error=:html_error
WHERE id=:id
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--gzip", action="store_true")
    ap.add_argument("--retry-errors", action="store_true")
    a = ap.parse_args()

    con = db()
    where = "html_path IS NULL OR html_bytes IS NULL"
    if a.retry_errors:
        where += " OR html_error IS NOT NULL"
    sql = (f"SELECT id, ctr_no, row_index FROM trials WHERE {where} "
           "ORDER BY ctr_no DESC")
    if a.limit:
        sql += f" LIMIT {a.limit}"
    todo = con.execute(sql).fetchall()
    (total,) = con.execute("SELECT COUNT(*) FROM trials").fetchone()
    (have,) = con.execute(
        "SELECT COUNT(*) FROM trials WHERE html_path IS NOT NULL").fetchone()

    if not total:
        print("[!] no trials in db - run harvest_index.py first", file=sys.stderr)
        return 1
    print(f"[+] {total} trials known, {have} pages already saved, {len(todo)} to do")
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
                rec = fetch_one(task, a.gzip)
                if rec is not None:
                    results.put(rec)
            finally:
                work.task_done()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(a.workers)]
    for t in threads:
        t.start()

    t0 = time.time()
    n_ok = n_err = n_bytes = done = 0
    pending = []
    try:
        while done < len(todo):
            try:
                rec = results.get(timeout=1.0)
            except queue.Empty:
                if not any(t.is_alive() for t in threads) and results.empty():
                    break
                continue
            done += 1
            pending.append(rec)
            if rec.get("html_error"):
                n_err += 1
                print(f"[!] {rec['id']}: {rec['html_error']}", flush=True)
            else:
                n_ok += 1
                n_bytes += rec["html_bytes"] or 0
            if len(pending) >= 25:
                con.executemany(UPSERT, pending)
                pending.clear()
            if done % 25 == 0 or done == len(todo):
                el = time.time() - t0
                rate = done / el if el else 0
                eta = (len(todo) - done) / rate if rate else 0
                print(f"[{done}/{len(todo)}] ok={n_ok} err={n_err} "
                      f"{n_bytes/1e6:.0f} MB | {rate*60:.0f} pages/min | "
                      f"ETA {eta/3600:.1f} h", flush=True)
    finally:
        if pending:
            con.executemany(UPSERT, pending)
        STOP.set()

    el = time.time() - t0
    print(f"\n[=] saved {n_ok} pages ({n_bytes/1e6:.0f} MB) in {el/60:.1f} min, "
          f"{n_err} errors")
    (left,) = con.execute(
        "SELECT COUNT(*) FROM trials WHERE html_path IS NULL").fetchone()
    if left:
        print(f"[i] {left} still pending - re-run this command to continue")
    if n_ok:
        print(f"[i] avg {n_bytes/n_ok/1024:.0f} KB/page; projected "
              f"{(n_bytes/n_ok)*total/1e9:.2f} GB for {total} pages"
              + (" (gzipped)" if a.gzip else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
