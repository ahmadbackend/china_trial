"""
Phase 3: turn out/trials.db into analysis-ready files.

Writes into out/export/:
  trials_index.csv    one row per trial: the list-page summary + doc path
  trials_flat.csv     one row per trial, one column per common detail field
  trials_full.jsonl   one JSON object per trial: all fields + sections + tables
  field_coverage.csv  how often each detail field occurs (pick columns from this)

Usage:
  python scraper/export.py
  python scraper/export.py --min-coverage 0.02   # widen trials_flat.csv
"""
import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

from common import ROOT, db

OUT = ROOT / "out" / "export"

INDEX_COLS = ["ctr_no", "id", "state", "drug_name", "indication", "popular_title",
              "list_page", "row_index", "doc_path", "doc_bytes", "doc_sha256",
              "fetched_at", "parsed_at", "error"]

# Fields promoted to the front of trials_flat.csv when present.
PREFERRED = [
    "登记号", "试验状态", "药物名称", "药物类型", "适应症", "备案号",
    "试验专业题目", "试验通俗题目", "试验方案编号", "方案最新版本号", "版本日期",
    "方案是否为联合用药", "申请人名称", "联系人姓名", "联系人座机", "联系人手机号",
    "联系人Email", "联系人邮政地址", "联系人邮编", "首次公示信息日期",
    "试验目的", "试验分类", "试验分期", "设计类型", "随机化", "盲法", "试验范围",
    "年龄", "性别", "健康受试者", "目标入组人数", "已入组人数", "实际入组总人数",
    "第一例受试者签署知情同意书日期", "第一例受试者入组日期", "试验完成日期",
    "数据安全监查委员会（DMC）", "是否购买保险",
]


def flatten(v, sep=" || "):
    if v is None:
        return ""
    if isinstance(v, list):
        return sep.join(str(x) for x in v)
    return str(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-coverage", type=float, default=0.01,
                    help="include a field column if it appears in >= this share")
    a = ap.parse_args()

    con = db()
    OUT.mkdir(parents=True, exist_ok=True)

    # ---------- index ----------
    rows = con.execute(f"SELECT {','.join(INDEX_COLS)} FROM trials "
                       "ORDER BY ctr_no DESC").fetchall()
    with (OUT / "trials_index.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(INDEX_COLS)
        w.writerows(rows)
    print(f"[+] trials_index.csv       {len(rows)} rows")

    # ---------- coverage pass ----------
    cnt = Counter()
    n_parsed = 0
    for (fj,) in con.execute("SELECT fields_json FROM trials "
                             "WHERE fields_json IS NOT NULL"):
        n_parsed += 1
        try:
            cnt.update(json.loads(fj).keys())
        except Exception:
            pass
    with (OUT / "field_coverage.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["field", "n_docs", "coverage"])
        for k, v in cnt.most_common():
            w.writerow([k, v, round(v / n_parsed, 4) if n_parsed else 0])
    print(f"[+] field_coverage.csv     {len(cnt)} distinct fields "
          f"over {n_parsed} parsed docs")

    if not n_parsed:
        print("[i] no parsed documents yet - run harvest_details.py")
        return 0

    keep = [k for k, v in cnt.most_common() if v / n_parsed >= a.min_coverage]
    cols = [k for k in PREFERRED if k in cnt] + [k for k in keep if k not in PREFERRED]

    # ---------- flat + full ----------
    q = ("SELECT id, ctr_no, state, drug_name, indication, popular_title, "
         "doc_path, fields_json, rows_json FROM trials "
         "WHERE fields_json IS NOT NULL ORDER BY ctr_no DESC")
    base = ["ctr_no", "id", "state", "doc_path"]
    n = 0
    with (OUT / "trials_flat.csv").open("w", newline="", encoding="utf-8-sig") as fc, \
         (OUT / "trials_full.jsonl").open("w", encoding="utf-8") as fj:
        w = csv.writer(fc)
        w.writerow(base + cols)
        for (tid, ctr, state, drug, ind, title, dpath, fjson, rjson) in con.execute(q):
            try:
                fields = json.loads(fjson)
            except Exception:
                continue
            sections = json.loads(rjson) if rjson else []
            w.writerow([ctr, tid, state, dpath] +
                       [flatten(fields.get(c)) for c in cols])
            fj.write(json.dumps({
                "ctr_no": ctr, "id": tid, "list_state": state,
                "list_drug_name": drug, "list_indication": ind,
                "list_popular_title": title, "doc_path": dpath,
                "fields": fields, "sections": sections,
            }, ensure_ascii=False) + "\n")
            n += 1
    print(f"[+] trials_flat.csv        {n} rows x {len(base)+len(cols)} cols")
    print(f"[+] trials_full.jsonl      {n} records")
    print(f"\n[=] files in {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
