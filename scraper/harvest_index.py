"""
Phase 1: walk the 1860 result pages of clinicaltrials.searchlist.dhtml and record
every trial's GUID + summary row into out/trials.db.

Resumable: pages already recorded in `pages` are skipped unless --refresh.

Usage:
  python scraper/harvest_index.py                 # all pages, 4 workers
  python scraper/harvest_index.py --workers 6
  python scraper/harvest_index.py --pages 1-50    # a slice
  python scraper/harvest_index.py --refresh       # redo pages already done
"""
import argparse, sys, threading, time
import concurrent.futures as cf
from datetime import datetime, timezone

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

from common import LIST_URL, Session, db, parse_list_page, row_sig, search_form, \
    PAGE_SIZE

_tls = threading.local()
_lock = threading.Lock()

# On re-scans, refresh the summary fields and flag the trial for re-download
# when its row changed since it was first recorded. first_seen is only set on
# the initial INSERT, so it always marks when the trial was first discovered.
_UPSET_COLS = """
  ctr_no=excluded.ctr_no, state=excluded.state, drug_name=excluded.drug_name,
  indication=excluded.indication, popular_title=excluded.popular_title,
  needs_refetch=CASE WHEN trials.row_sig IS NOT NULL
                       AND trials.row_sig <> excluded.row_sig
                     THEN 1 ELSE trials.needs_refetch END,
  row_sig=excluded.row_sig
"""

# Partial save: rows from a page that is being flagged for retry.
UPSERT_PARTIAL = """
INSERT INTO trials(id,ctr_no,state,drug_name,indication,popular_title,
                   list_page,row_index,row_sig,first_seen)
VALUES(:id,:ctr_no,:state,:drug_name,:indication,:popular_title,:page,
       :row_index,:sig,:seen)
ON CONFLICT(id) DO UPDATE SET
""" + _UPSET_COLS

# Full save: a complete page, so list position is refreshed too.
UPSERT_FULL = """
INSERT INTO trials(id,ctr_no,state,drug_name,indication,popular_title,
                   list_page,row_index,row_sig,first_seen)
VALUES(:id,:ctr_no,:state,:drug_name,:indication,:popular_title,:page,
       :row_index,:sig,:seen)
ON CONFLICT(id) DO UPDATE SET
  list_page=excluded.list_page, row_index=excluded.row_index,
""" + _UPSET_COLS


def sess():
    if not hasattr(_tls, "s"):
        _tls.s = Session()
    return _tls.s


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def probe_total():
    r = sess().post(LIST_URL, search_form(1), expect="html")
    rows, total, pages = parse_list_page(r.text)
    return rows, total, pages


LAST_PAGE = None   # set in main(); the only page allowed to be short


def do_page(page):
    try:
        r = sess().post(LIST_URL, search_form(page), expect="html")
        rows, _, _ = parse_list_page(r.text)
        if not rows:
            return page, 0, "no rows parsed"
        # A short page means rows were missed - either a partial response or a
        # row shape the parser does not recognise. Flag it rather than silently
        # banking an incomplete page (this is how ~7.5k trials went missing
        # when suffixed "..._p" ids were not matched).
        if len(rows) < PAGE_SIZE and page != LAST_PAGE:
            return page, rows, f"short page: {len(rows)}/{PAGE_SIZE} rows"
        return page, rows, None
    except Exception as e:
        return page, None, f"{type(e).__name__}: {e}"[:300]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--pages", help="slice like 1-50 (default: all)")
    ap.add_argument("--refresh", action="store_true", help="re-fetch pages already done")
    a = ap.parse_args()

    global LAST_PAGE
    con = db()
    print("[*] probing page 1 for totals ...", flush=True)
    rows1, total, npages = probe_total()
    LAST_PAGE = npages
    print(f"[+] site reports {total} records across {npages} pages ({PAGE_SIZE}/page)")
    if not npages:
        print("[!] could not read pagination; aborting", file=sys.stderr)
        return 1

    if a.pages:
        lo, _, hi = a.pages.partition("-")
        want = range(int(lo), int(hi or lo) + 1)
    else:
        want = range(1, npages + 1)

    done = {p for (p,) in con.execute("SELECT page FROM pages WHERE error IS NULL")}
    todo = [p for p in want if a.refresh or p not in done]
    print(f"[+] {len(done)} pages already stored; {len(todo)} to fetch")
    if not todo:
        print("[=] index already complete")
        return 0

    t0 = time.time()
    n_ok = n_rows = n_err = 0

    def store(page, rows, err):
        nonlocal n_ok, n_rows, n_err
        with _lock:
            # Keep whatever rows came back even when the page is flagged, then
            # mark it for retry. The ids are a primary key, so re-fetching the
            # page later simply fills in the rest.
            if isinstance(rows, list) and rows:
                seen = now()
                stamped = [dict(r, page=page, sig=row_sig(r), seen=seen)
                           for r in rows]
                if err:
                    con.executemany(UPSERT_PARTIAL, stamped)
                else:
                    con.executemany(UPSERT_FULL, stamped)
                n_rows += len(rows)
            if err or not isinstance(rows, list):
                con.execute("INSERT OR REPLACE INTO pages(page,n_ids,fetched_at,error)"
                            " VALUES(?,?,?,?)",
                            (page, len(rows) if isinstance(rows, list) else 0,
                             now(), err))
                n_err += 1
                print(f"[!] page {page}: {err}", flush=True)
                return
            con.execute("INSERT OR REPLACE INTO pages(page,n_ids,fetched_at,error)"
                        " VALUES(?,?,?,NULL)", (page, len(rows), now()))
            n_ok += 1

    with cf.ThreadPoolExecutor(a.workers) as ex:
        for i, (page, rows, err) in enumerate(ex.map(do_page, todo), 1):
            store(page, rows, err)
            if i % 20 == 0 or i == len(todo):
                el = time.time() - t0
                rate = i / el if el else 0
                eta = (len(todo) - i) / rate if rate else 0
                (tot,) = con.execute("SELECT COUNT(*) FROM trials").fetchone()
                print(f"[{i}/{len(todo)}] pages ok={n_ok} err={n_err} "
                      f"trials={tot} | {rate*60:.0f} pages/min | ETA {eta/60:.0f} min",
                      flush=True)

    (tot,) = con.execute("SELECT COUNT(*) FROM trials").fetchone()
    (badp,) = con.execute("SELECT COUNT(*) FROM pages WHERE error IS NOT NULL").fetchone()
    print(f"\n[=] done in {(time.time()-t0)/60:.1f} min")
    print(f"[=] distinct trials in db: {tot} (site says {total})")
    if badp:
        print(f"[!] {badp} pages errored - rerun the same command to retry them")
    return 0


if __name__ == "__main__":
    sys.exit(main())
