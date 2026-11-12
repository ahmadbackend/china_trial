"""
Integrity + completeness audit of everything downloaded so far. Offline.

For every stored document it checks:
  * the file exists and decompresses
  * sha256 and byte count still match what was recorded at download time
  * the WordML is well formed and ends with its closing tag (not truncated)
  * the CTR number inside the document matches the one in the index
  * the parse yields a sane number of fields and all expected sections

    python scraper/verify.py            # audit every downloaded document
    python scraper/verify.py --sample 500
"""
import argparse
import gzip
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

from common import ROOT, db           # noqa: E402
from parse_doc import parse_wordml    # noqa: E402

CORE = ["登记号", "试验状态", "药物名称", "药物类型", "适应症",
        "试验专业题目", "申请人名称"]


def read_doc(path):
    raw = Path(path).read_bytes()
    return gzip.decompress(raw) if str(path).endswith(".gz") else raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, help="check only the first N")
    a = ap.parse_args()

    con = db()
    q = ("SELECT ctr_no, id, doc_path, doc_bytes, doc_sha256 FROM trials "
         "WHERE doc_path IS NOT NULL ORDER BY ctr_no DESC")
    if a.sample:
        q += f" LIMIT {a.sample}"
    rows = con.execute(q).fetchall()
    if not rows:
        print("[!] nothing downloaded yet")
        return 1
    print(f"auditing {len(rows)} documents ...\n")

    bad = []
    fieldcount = []
    missing_core = Counter()
    sections = Counter()
    total_raw = total_doc = 0

    for i, (ctr, tid, dpath, dbytes, dsha) in enumerate(rows, 1):
        p = ROOT / "out" / dpath if not Path(dpath).is_absolute() else Path(dpath)
        try:
            total_raw += p.stat().st_size
            doc = read_doc(p)
            total_doc += len(doc)
            if len(doc) != dbytes:
                bad.append((ctr, f"size {len(doc)} != recorded {dbytes}"))
                continue
            if hashlib.sha256(doc).hexdigest() != dsha:
                bad.append((ctr, "sha256 mismatch - file changed since download"))
                continue
            if not doc.rstrip().endswith(b"</w:wordDocument>"):
                bad.append((ctr, "truncated: missing closing tag"))
                continue
            rec = parse_wordml(doc)
            if rec["ctr_no"] != ctr:
                bad.append((ctr, f"document is for {rec['ctr_no']}"))
                continue
            fieldcount.append(len(rec["fields"]))
            for k in CORE:
                if not rec["fields"].get(k):
                    missing_core[k] += 1
            sections[len([s for s in rec["sections"] if s["heading"]])] += 1
        except Exception as e:
            bad.append((ctr, f"{type(e).__name__}: {e}"[:120]))
        if i % 500 == 0:
            print(f"  {i}/{len(rows)} ...", flush=True)

    ok = len(rows) - len(bad)
    print("\n" + "=" * 60)
    print(f"  documents audited      {len(rows)}")
    print(f"  intact and parseable   {ok}")
    print(f"  problems               {len(bad)}")
    if fieldcount:
        fieldcount.sort()
        print(f"  fields per document    min {fieldcount[0]}  "
              f"median {fieldcount[len(fieldcount)//2]}  max {fieldcount[-1]}")
    print(f"  section counts seen    {dict(sorted(sections.items()))}")
    if missing_core:
        print("  documents missing a core field:")
        for k, v in missing_core.most_common():
            print(f"      {k}: {v}")
    else:
        print("  every document has all core fields")
    if total_raw:
        print(f"  on disk {total_raw/1e9:.2f} GB  (uncompressed "
              f"{total_doc/1e9:.2f} GB, {total_doc/total_raw:.1f}x)")
    print("=" * 60)
    if bad:
        print("\nproblems (first 25):")
        for c, m in bad[:25]:
            print(f"  {c}: {m}")
        print("\nRe-fetch these with:  python scraper/harvest_details.py --retry-errors")
        ids = [r[1] for r in rows if r[0] in {b[0] for b in bad}]
        (ROOT / "out" / "verify_failures.json").write_text(
            json.dumps(ids, indent=2), encoding="utf-8")
        print("ids written to out/verify_failures.json")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
