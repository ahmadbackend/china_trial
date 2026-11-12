"""
Offline self-test. Runs with NO network and NO VPN: it validates the parsers
against the fixtures in the recorded journey, then exercises the database and
export paths on a temporary copy.

Run this before switching the VPN on, and again if anything is changed.

    python scraper/selftest.py
"""
import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import ROOT, parse_list_page, search_form, SCHEMA  # noqa: E402
from parse_doc import parse_wordml                             # noqa: E402

CAPTURE = next(ROOT.glob("capture_www.chinadrugtrials.org.cn_*"), None)
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
    return cond


def test_form():
    print("\n[1] search form field set")
    f = search_form(7)
    # the exact set the site's #searchfrm posts, from the HAR
    expect = {"id", "ckm_index", "sort", "sort2", "rule", "secondLevel", "currentpage",
              "keywords", "reg_no", "indication", "case_no", "drugs_name", "drugs_type",
              "appliers", "communities", "researchers", "agencies", "state"}
    check("form has exactly the site's fields", set(f) == expect,
          f"extra={set(f)-expect} missing={expect-set(f)}")
    check("currentpage is honoured", f["currentpage"] == "7")


def test_list_parsing():
    print("\n[2] result-list parsing (fixture from the recorded journey)")
    if not CAPTURE:
        check("capture directory present", False, "no capture_* dir found")
        return
    html_dir = CAPTURE / "html" / "www.chinadrugtrials.org.cn"
    files = sorted(html_dir.glob("clinicaltrials.searchlist*.html"))
    if not check("list fixtures found", bool(files), f"{len(files)} file(s)"):
        return
    for fp in files:
        html = fp.read_text(encoding="utf-8", errors="replace")
        rows, total, pages = parse_list_page(html)
        check(f"{fp.name}: 20 rows parsed", len(rows) == 20, f"got {len(rows)}")
        check(f"{fp.name}: totals read", total == 37197 and pages == 1860,
              f"total={total} pages={pages}")
        if rows:
            r = rows[0]
            check(f"{fp.name}: 32-hex guid", len(r["id"]) == 32, r["id"])
            check(f"{fp.name}: CTR number", r["ctr_no"].startswith("CTR"), r["ctr_no"])
            check(f"{fp.name}: all rows have ids",
                  all(len(x["id"]) == 32 for x in rows))
            check(f"{fp.name}: ids are distinct",
                  len({x["id"] for x in rows}) == len(rows))
            check(f"{fp.name}: no html left in cells",
                  not any("<" in x["drug_name"] for x in rows))


def test_suffixed_ids():
    print("\n[2b] rows whose id carries a suffix (regression: '..._p')")
    fx = ROOT / "fixtures" / "listpage_suffixed_ids.html"
    if not check("suffixed-id fixture present", fx.exists(), str(fx)):
        return
    rows, total, pages = parse_list_page(
        fx.read_text(encoding="utf-8", errors="replace"))
    check("all 20 rows parsed, not just the bare ids", len(rows) == 20,
          f"got {len(rows)}")
    sfx = [r for r in rows if not re.fullmatch(r"[0-9a-f]{32}", r["id"])]
    check("suffixed ids are captured", len(sfx) >= 15, f"{len(sfx)} suffixed")
    check("suffixed ids keep their suffix verbatim",
          all(r["id"].startswith(tuple("0123456789abcdef")) and "_" in r["id"]
              for r in sfx), str([r["id"] for r in sfx[:2]]))
    check("every row still has a CTR number",
          all(r["ctr_no"].startswith("CTR") for r in rows))
    check("ids remain distinct", len({r["id"] for r in rows}) == 20)


def test_doc_parsing():
    print("\n[3] WordML detail parsing (fixture .doc from the journey)")
    docs = sorted((ROOT / "downloaded_files").glob("*.doc"))
    if not check("sample .doc found", bool(docs), f"{len(docs)} file(s)"):
        return None
    rec = parse_wordml(docs[0].read_bytes())
    check("CTR number extracted", bool(rec["ctr_no"])
          and rec["ctr_no"].startswith("CTR") and len(rec["ctr_no"]) == 11,
          str(rec["ctr_no"]))
    check("rows extracted", rec["n_rows"] > 50, f"{rec['n_rows']} rows")
    check("fields extracted", len(rec["fields"]) > 30, f"{len(rec['fields'])} fields")
    f = rec["fields"]
    for key in ("登记号", "试验状态", "药物名称", "药物类型", "适应症",
                "试验专业题目", "申请人名称", "试验分类", "设计类型", "随机化"):
        check(f"field present: {key}", key in f and f[key] != "")
    check("试验目的 prose captured", isinstance(f.get("试验目的"), str)
          and len(f.get("试验目的", "")) > 50)
    check("入选标准 is a full list", isinstance(f.get("入选标准"), list)
          and len(f["入选标准"]) >= 3, str(type(f.get("入选标准"))))
    check("排除标准 keeps every item", isinstance(f.get("排除标准"), list)
          and len(f["排除标准"]) >= 20, f"{len(f.get('排除标准', []))} items")
    check("HTML entity decoded (&reg; -> (R))", "&reg;" not in json.dumps(
        rec["sections"], ensure_ascii=False) and "&#174;" not in json.dumps(
        rec["sections"], ensure_ascii=False))

    heads = [s["heading"] for s in rec["sections"]]
    check("7 numbered sections found",
          sum(1 for h in heads if h.startswith(("一", "二", "三", "四", "五", "六", "七"))) == 7,
          str(heads))
    tables = [t for s in rec["sections"] for ss in s["subsections"] for t in ss["tables"]]
    check("no None in tables (regression)", all(t is not None for t in tables))
    check("embedded tables captured", len(tables) >= 5, f"{len(tables)} tables")
    check("tables carry headers", all(t["header"] for t in tables))
    sites = [t for t in tables if "机构名称" in t["header"]]
    check("site table has data rows", bool(sites) and len(sites[0]["rows"]) >= 1,
          str(sites[0]["rows"][:1]) if sites else "none")
    recs = [r for s in rec["sections"] for ss in s["subsections"] for r in ss["records"]]
    multi = [ss for s in rec["sections"] for ss in s["subsections"]
             if len(ss["records"]) > 1]
    check("repeated investigator blocks split into records", bool(multi),
          f"{len(recs)} records total")
    check("full text available", len(rec["text"]) > 500, f"{len(rec['text'])} chars")
    return rec


def test_db_and_export(rec):
    print("\n[4] database + export round-trip (temp dir)")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "t.db"
        con = sqlite3.connect(str(p), isolation_level=None)
        con.executescript(SCHEMA)
        con.execute(
            "INSERT INTO trials(id,ctr_no,state,drug_name,indication,popular_title,"
            "list_page,row_index) VALUES(?,?,?,?,?,?,?,?)",
            ("0" * 32, "CTR20264207", "进行中", "x", "y", "z", 1, 1))
        # idempotent upsert, as harvest_index does on a re-run
        con.execute(
            "INSERT INTO trials(id,ctr_no,state,list_page,row_index) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET state=excluded.state",
            ("0" * 32, "CTR20264207", "已完成", 1, 1))
        (n,) = con.execute("SELECT COUNT(*) FROM trials").fetchone()
        check("upsert does not duplicate ids", n == 1, f"{n} rows")
        (st,) = con.execute("SELECT state FROM trials").fetchone()
        check("upsert refreshes the row", st == "已完成", st)
        if rec:
            con.execute("UPDATE trials SET fields_json=?, rows_json=?, parsed_at='t'",
                        (json.dumps(rec["fields"], ensure_ascii=False),
                         json.dumps(rec["sections"], ensure_ascii=False)))
            (fj,) = con.execute("SELECT fields_json FROM trials").fetchone()
            back = json.loads(fj)
            check("fields survive the json round-trip",
                  back.get("登记号") == rec["fields"].get("登记号"))
        con.close()


def main():
    print("=" * 66)
    print("chinadrugtrials harvester - offline self-test (no network needed)")
    print("=" * 66)
    print(f"capture: {CAPTURE.name if CAPTURE else 'MISSING'}")
    test_form()
    test_list_parsing()
    test_suffixed_ids()
    rec = test_doc_parsing()
    test_db_and_export(rec)
    print("\n" + "=" * 66)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
