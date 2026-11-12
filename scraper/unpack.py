"""
Turn the stored .doc.gz files back into ordinary .doc files that Word opens,
named the way the website names them (e.g. CTR20264207详细信息.doc).

    python scraper/unpack.py CTR20263297                  # one trial
    python scraper/unpack.py CTR20263297 CTR20264286      # several
    python scraper/unpack.py --all                        # everything
    python scraper/unpack.py --all --out D:\\trial_docs    # somewhere else
    python scraper/unpack.py --state 已完成                # only completed trials
    python scraper/unpack.py --drug 维立西呱                # match on drug name
    python scraper/unpack.py --all --dry-run              # just report the size

Nothing is deleted: the .gz originals stay where they are.
"""
import argparse
import gzip
import hashlib
import sys
from pathlib import Path

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

from common import ROOT, db   # noqa: E402

DEFAULT_OUT = ROOT / "out" / "docs_unpacked"
SUFFIX = "详细信息"          # what the site appends to the CTR number


def resolve(dpath):
    p = Path(dpath)
    return p if p.is_absolute() else ROOT / "out" / dpath


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ctr", nargs="*", help="CTR numbers to unpack")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--state", help="filter on trial status")
    ap.add_argument("--drug", help="filter on drug name (substring)")
    ap.add_argument("--plain-name", action="store_true",
                    help="name files CTR#########.doc without the Chinese suffix")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not (a.ctr or a.all or a.state or a.drug):
        ap.error("give CTR numbers, or --all, or --state/--drug")

    con = db()
    q = "SELECT ctr_no, doc_path, doc_bytes, doc_sha256 FROM trials WHERE doc_path IS NOT NULL"
    params = []
    if a.ctr:
        q += f" AND ctr_no IN ({','.join('?' * len(a.ctr))})"
        params += a.ctr
    if a.state:
        q += " AND state LIKE ?"
        params.append(f"%{a.state}%")
    if a.drug:
        q += " AND drug_name LIKE ?"
        params.append(f"%{a.drug}%")
    q += " ORDER BY ctr_no DESC"
    rows = con.execute(q, params).fetchall()

    if not rows:
        print("[!] nothing matched (is it downloaded yet?)")
        return 1

    est = sum(r[2] or 0 for r in rows)
    print(f"{len(rows)} document(s), {est/1e9:.2f} GB once unpacked")
    if a.dry_run:
        return 0

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    ok = skipped = 0
    bad = []
    for i, (ctr, dpath, dbytes, dsha) in enumerate(rows, 1):
        src = resolve(dpath)
        name = f"{ctr}.doc" if a.plain_name else f"{ctr}{SUFFIX}.doc"
        dst = out / name
        try:
            raw = src.read_bytes()
            doc = gzip.decompress(raw) if src.suffix == ".gz" else raw
            # the .gz is lossless, so this must match the download-time hash
            if dsha and hashlib.sha256(doc).hexdigest() != dsha:
                bad.append((ctr, "sha256 mismatch"))
                continue
            if dst.exists() and dst.stat().st_size == len(doc):
                skipped += 1
                continue
            dst.write_bytes(doc)
            ok += 1
        except Exception as e:
            bad.append((ctr, f"{type(e).__name__}: {e}"[:100]))
        if len(rows) > 200 and i % 500 == 0:
            print(f"  {i}/{len(rows)} ...", flush=True)

    print(f"\n[=] wrote {ok}, already present {skipped}, failed {len(bad)}")
    print(f"[=] into {out}")
    for c, m in bad[:10]:
        print(f"  !! {c}: {m}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
