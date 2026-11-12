"""
WordML (the .doc served by ?_export=doc) -> structured records.

The export is Word 2003 XML. Layout discovered from real documents:

  * <w:p> paragraphs BETWEEN tables carry the top-level headings
    ("一、题目和背景信息", "二、申请人信息", ...).
  * <w:tbl> rows carry everything else:
      - 1 cell                     -> a sub-heading ("2、试验设计") or body prose
      - 2 cells                    -> label / value; an EMPTY label continues the
                                      previous label (numbered criteria lists)
      - 4 cells                    -> usually TWO label/value pairs side by side
      - row whose 1st or 2nd cell
        is "序号"                  -> header of an embedded list table
                                      (test drugs, endpoints, sites, ethics)
  * Values are wrapped in CDATA, so a cell's text is the concatenation of every
    <w:t> inside it. That also repairs labels the exporter splits across runs.

Two gotchas handled here:
  1. The exporter emits HTML entities (&reg; &nbsp;) that are undefined in XML.
     Inside CDATA they are literal text, so they are fixed up AFTER extraction
     with html.unescape as well as neutralised before the parse.
  2. Repeated label blocks (several principal investigators) are split into
     records rather than collapsed.

Public API:
    parse_wordml(data: bytes|str) -> dict
    parse_file(path)              -> dict
"""
import html
import json
import re
import sys
from html.entities import name2codepoint
from pathlib import Path

from lxml import etree

W = "http://schemas.microsoft.com/office/word/2003/wordml"
T = f"{{{W}}}t"

_XML_OK = {"amp", "lt", "gt", "quot", "apos"}
_ENT_RE = re.compile(rb"&([A-Za-z][A-Za-z0-9]{1,31});")

SEC_RE = re.compile(r"^[一二三四五六七八九十]+、")
SUB_RE = re.compile(r"^\d+、")
INDEX_LABELS = ("序号",)


def _fix_entities(data: bytes) -> bytes:
    """Neutralise named entities that are undefined in XML (&reg;, &nbsp; ...)."""
    def sub(m):
        name = m.group(1).decode("ascii")
        if name in _XML_OK:
            return m.group(0)
        cp = name2codepoint.get(name)
        if cp is None:
            return b"&amp;" + m.group(1) + b";"
        return f"&#{cp};".encode("ascii")
    return _ENT_RE.sub(sub, data)


def _norm(s: str) -> str:
    s = html.unescape(s or "").replace("\xa0", " ").replace("　", " ")
    return re.sub(r"[ \t]+", " ", s).strip()


def _runs_text(el) -> str:
    return _norm("".join(t.text or "" for t in el.iter(T)))


def _in_table(el):
    p = el.getparent()
    while p is not None:
        if isinstance(p.tag, str) and p.tag == f"{{{W}}}tbl":
            return True
        p = p.getparent()
    return False


def _blocks(root):
    """Document order stream of ('p', text) and ('row', [cells]).

    The paragraphs and tables live under w:body/w:sect (not directly under
    w:body), and deeper nesting is possible, so this scans by tag in document
    order and skips anything already covered by an enclosing table.
    """
    for el in root.iter(f"{{{W}}}p", f"{{{W}}}tbl"):
        if _in_table(el):
            continue  # paragraphs inside cells are read via the row
        if el.tag == f"{{{W}}}p":
            txt = _runs_text(el)
            if txt:
                yield "p", txt
        else:
            for tr in el.iter(f"{{{W}}}tr"):
                cells = [_runs_text(tc) for tc in tr.findall(f"{{{W}}}tc")]
                if any(cells):
                    yield "row", cells


def _is_index_header(cells):
    """A list-table header: ['序号', ...] or ['试验药', '序号', ...]."""
    if len(cells) < 2:
        return None
    if cells[0] in INDEX_LABELS:
        return "", cells
    if len(cells) >= 3 and cells[1] in INDEX_LABELS:
        return cells[0], cells[1:]
    return None


def _split_records(kv):
    """[(label, value)] -> [ {label: value} ], split where the 1st label repeats."""
    if not kv:
        return []
    recs, cur, first = [], {}, kv[0][0]
    for label, value in kv:
        if label == first and cur:
            recs.append(cur)
            cur = {}
        if label in cur:
            prev = cur[label]
            cur[label] = prev + [value] if isinstance(prev, list) else [prev, value]
        else:
            cur[label] = value
    if cur:
        recs.append(cur)
    return recs


def parse_wordml(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    root = etree.fromstring(
        _fix_entities(data),
        parser=etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False),
    )
    if root is None:
        raise ValueError("could not parse WordML")

    raw_rows = []
    sections = []
    sec = None
    sub = None
    table = None          # open list table
    last_label = None     # for empty-label continuation rows

    def new_section(heading):
        nonlocal sec, sub, table, last_label
        sec = {"heading": heading, "subsections": []}
        sections.append(sec)
        sub = table = last_label = None

    def new_sub(heading):
        nonlocal sub, table, last_label
        if sec is None:
            new_section("")
        sub = {"heading": heading, "kv": [], "tables": [], "text": []}
        sec["subsections"].append(sub)
        table = last_label = None

    def cur_sub():
        if sub is None:
            new_sub("")
        return sub

    def add_kv(label, value):
        nonlocal last_label
        label = label.rstrip(":：")
        if not label:
            return
        cur_sub()["kv"].append((label, value))
        last_label = label

    full_text = []   # every block in document order, for full-text search

    for kind, payload in _blocks(root):
        full_text.append(payload if kind == "p"
                         else "\t".join(c for c in payload if c))
        if kind == "p":
            if SEC_RE.match(payload):
                new_section(payload)
            elif SUB_RE.match(payload):
                new_sub(payload)
            elif payload:
                cur_sub()["text"].append(payload)
            continue

        cells = payload
        raw_rows.append(cells)
        nonempty = [c for c in cells if c]

        # --- embedded list table -------------------------------------------
        hdr = _is_index_header(cells)
        if hdr is not None:
            owner, header = hdr
            # NOTE: resolve the subsection FIRST - cur_sub() may open a new one,
            # which resets `table`, and would otherwise append None here.
            holder = cur_sub()["tables"]
            table = {"owner": owner, "header": header, "rows": []}
            holder.append(table)
            continue
        if table is not None and not cells[0] and len(cells) >= 3 and len(nonempty) >= 2:
            table["rows"].append(cells[1:])
            continue
        if table is not None and cells[0] and len(cells) >= 3 and cells[0].rstrip(".").isdigit():
            table["rows"].append(cells)
            continue

        # --- a genuinely single-cell row: heading or prose -------------------
        if len(cells) == 1:
            txt = cells[0]
            if SEC_RE.match(txt):
                new_section(txt)
            elif SUB_RE.match(txt):
                new_sub(txt)
            else:
                cur_sub()["text"].append(txt)
            continue

        # --- continuation row: empty label, value present -------------------
        # (the numbered 入选标准 / 排除标准 criteria arrive this way). This must
        # be tested before the prose branch or each criterion is lost as text.
        if not cells[0] and len(nonempty) >= 1:
            val = " ".join(c for c in cells[1:] if c)
            if val and last_label:
                cur_sub()["kv"].append((last_label, val))
            elif val:
                cur_sub()["text"].append(val)
            continue

        # --- label with an empty value (e.g. 相关登记号) ---------------------
        if cells[0] and not nonempty[1:]:
            if SEC_RE.match(cells[0]):
                new_section(cells[0])
            elif SUB_RE.match(cells[0]) and len(cells) == 1:
                new_sub(cells[0])
            else:
                add_kv(cells[0], "")
            continue

        # --- label / value rows ---------------------------------------------
        table = None
        if len(cells) >= 4 and cells[0] and cells[2]:
            # two side-by-side pairs: 登记号|CTR...|试验状态|进行中
            pairs = list(zip(cells[0::2], cells[1::2]))
            if all(k for k, _ in pairs):
                for k, v in pairs:
                    add_kv(k, v)
                continue

        if cells[0]:
            add_kv(cells[0], " ".join(c for c in cells[1:] if c))
        elif last_label:
            # continuation of the previous label (numbered criteria)
            val = " ".join(c for c in cells[1:] if c)
            if val:
                cur_sub()["kv"].append((last_label, val))

    # ---------- derive convenience views ----------
    fields = {}        # plain label -> value (first wins; list when it differs)
    scoped = {}        # "section|subsection|label" -> value
    for s in sections:
        for ss in s["subsections"]:
            for label, value in ss["kv"]:
                key = f"{s['heading']}|{ss['heading']}|{label}"
                if key in scoped:
                    prev = scoped[key]
                    scoped[key] = prev + [value] if isinstance(prev, list) else [prev, value]
                else:
                    scoped[key] = value
                # the flat view drops the "6、" style numbering from labels
                flat = SUB_RE.sub("", label).strip() or label
                if flat in fields:
                    prev = fields[flat]
                    if isinstance(prev, list):
                        if value not in prev:
                            prev.append(value)
                    elif prev != value:
                        fields[flat] = [prev, value]
                else:
                    fields[flat] = value
            # a subsection that is nothing but prose (試驗目的, 試驗狀態 ...)
            # becomes a field named after its own heading
            if ss["text"] and not ss["kv"] and ss["heading"]:
                name = SUB_RE.sub("", ss["heading"]).strip()
                if name and name not in fields:
                    fields[name] = "\n".join(ss["text"])
            ss["records"] = _split_records(ss["kv"])
            ss["kv"] = [list(p) for p in ss["kv"]]  # JSON-friendly

    text = "\n".join(full_text)

    ctr = fields.get("登记号")
    if isinstance(ctr, list):
        ctr = ctr[0]
    if ctr:
        m = re.search(r"CTR\d{8}", ctr)
        ctr = m.group(0) if m else ctr
    if not ctr:
        m = re.search(rb"CTR\d{8}", data)
        ctr = m.group(0).decode() if m else None

    return {"ctr_no": ctr, "fields": fields, "scoped": scoped,
            "sections": sections, "rows": raw_rows, "text": text,
            "n_rows": len(raw_rows)}


def parse_file(path):
    return parse_wordml(Path(path).read_bytes())


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    rec = parse_file(sys.argv[1])
    print(f"ctr_no   : {rec['ctr_no']}")
    print(f"rows     : {rec['n_rows']}   fields: {len(rec['fields'])}")
    for s in rec["sections"]:
        print(f"\n## {s['heading']}")
        for ss in s["subsections"]:
            print(f"   ### {ss['heading'] or '(none)'}"
                  f"  kv={len(ss['kv'])} tables={len(ss['tables'])}"
                  f" recs={len(ss['records'])}")
            for label, value in ss["kv"][:4]:
                print(f"       - {label}: {value[:60]}")
            for t in ss["tables"]:
                print(f"       [table owner={t['owner']!r} header={t['header']}"
                      f" rows={len(t['rows'])}]")
    if "--json" in sys.argv:
        Path("sample_parsed.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\nwrote sample_parsed.json")
