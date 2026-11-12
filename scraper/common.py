"""Shared session / WAF / storage layer for the chinadrugtrials.org.cn harvester."""
import json, re, sqlite3, subprocess, sys, threading, time
from pathlib import Path

from curl_cffi import requests as cr

BASE = "https://www.chinadrugtrials.org.cn"
LIST_URL = f"{BASE}/clinicaltrials.searchlist.dhtml"
DETAIL_URL = f"{BASE}/clinicaltrials.searchlistdetail.dhtml"
DOC_URL = f"{DETAIL_URL}?_export=doc"

ROOT = Path(__file__).resolve().parent.parent
SESSION_FILE = ROOT / "out" / "session.json"
DB_FILE = ROOT / "out" / "trials.db"
DOCS_DIR = ROOT / "out" / "docs"
HTML_DIR = ROOT / "out" / "html"

# NOTE: the *real* pages also carry the WAF's <meta id="9DhefwqGPrzGxEp9hPaoag">
# script, so that marker cannot be used to spot a challenge. The reliable signals
# are: HTTP 202, or a 200 whose body lacks the content we asked for.
LIST_MARKER = "searchfrm"      # only present on a genuine result page
# The detail page uses a different template and has NO searchfrm, so it needs
# its own marker. (The WAF also rotates the id on its challenge <meta>, e.g.
# 9DhefwqGPrzGxEp9hPaoag and JLyKZlWgYjpTkAsEt9LnA, which is another reason
# detection keys on expected content being present rather than on a marker.)
DETAIL_MARKER = "一、题目和背景信息"
PAGE_SIZE = 20                 # server-fixed; a `pagesize` field is accepted but ignored


def search_form(page=1, **over):
    """The exact field set the site's #searchfrm posts."""
    d = {"id": "", "ckm_index": "", "sort": "desc", "sort2": "", "rule": "CTR",
         "secondLevel": "0", "currentpage": str(page), "keywords": "", "reg_no": "",
         "indication": "", "case_no": "", "drugs_name": "", "drugs_type": "",
         "appliers": "", "communities": "", "researchers": "", "agencies": "", "state": ""}
    d.update({k: str(v) for k, v in over.items()})
    return d


class ChallengeError(RuntimeError):
    pass


class Session:
    """curl_cffi session bound to nodriver-minted WAF cookies, with auto re-bootstrap."""

    _boot_lock = threading.Lock()
    _boot_stamp = 0.0

    def __init__(self, impersonate="chrome"):
        self.impersonate = impersonate
        self._mtime = 0.0
        self._load()

    # ---------- session bundle ----------
    def _load(self):
        if not SESSION_FILE.exists():
            self.bootstrap(force=True)
        b = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        self.bundle = b
        self._mtime = SESSION_FILE.stat().st_mtime
        self.s = cr.Session(impersonate=self.impersonate, headers=b["headers"],
                            cookies=b["cookies"], timeout=(30, 180))

    def _reload_if_stale(self):
        try:
            if SESSION_FILE.stat().st_mtime > self._mtime:
                self._load()
                return True
        except FileNotFoundError:
            pass
        return False

    @classmethod
    def bootstrap(cls, force=False, headless=False):
        """Re-run nodriver to mint fresh WAF cookies. Serialised across threads."""
        with cls._boot_lock:
            if not force and time.time() - cls._boot_stamp < 90:
                return  # another thread just refreshed it
            cmd = [sys.executable, "-X", "utf8", str(ROOT / "scraper" / "bootstrap.py"),
                   "--out", str(SESSION_FILE)]
            if headless:
                cmd.append("--headless")
            print("[waf] re-bootstrapping session with nodriver ...", flush=True)
            r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=420)
            if r.returncode != 0:
                raise RuntimeError(f"bootstrap failed:\n{r.stdout}\n{r.stderr}")
            cls._boot_stamp = time.time()
            print("[waf] session refreshed", flush=True)

    # ---------- requests ----------
    def post(self, url, data, expect="html", tries=6):
        """POST with WAF-challenge detection, re-bootstrap and backoff.

        Two failure classes are handled differently:
          ChallengeError -> cookies are stale, mint new ones with nodriver
          other errors   -> transient (timeout / truncation / 5xx), plain retry
        """
        last = None
        for attempt in range(1, tries + 1):
            try:
                r = self.s.post(url, data=data)
                if r.status_code == 202:
                    raise ChallengeError("WAF challenge (HTTP 202)")
                if r.status_code in (401, 403):
                    raise ChallengeError(f"WAF rejected session (HTTP {r.status_code})")
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")

                ctype = r.headers.get("content-type", "")
                if expect == "doc":
                    body = r.content
                    if body.lstrip().startswith(b"<?xml"):
                        if not body.rstrip().endswith(b"</w:wordDocument>"):
                            # partial transfer: very common on a slow link
                            raise RuntimeError(f"truncated doc ({len(body)} B)")
                        return r
                    if "html" in ctype:
                        raise ChallengeError("got HTML where WordML expected")
                    raise RuntimeError(f"unexpected ctype {ctype} ({len(body)} B)")

                if expect == "html":
                    if LIST_MARKER in r.text:
                        return r
                    raise ChallengeError(f"result page missing {LIST_MARKER!r}")

                if expect == "detail":
                    if DETAIL_MARKER in r.text:
                        return r
                    raise ChallengeError(f"detail page missing {DETAIL_MARKER!r}")
                return r
            except ChallengeError as e:
                last = e
                # a sibling thread may already have refreshed the bundle
                if not self._reload_if_stale():
                    self.bootstrap()
                    self._load()
                time.sleep(2 * attempt)
            except Exception as e:
                last = e
                time.sleep(min(3 * attempt, 20))
        raise RuntimeError(f"giving up after {tries} tries: {last}")


# ---------- storage ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
  id            TEXT PRIMARY KEY,   -- 32-hex GUID used by the detail endpoint
  ctr_no        TEXT,               -- registration no.  e.g. CTR20264207
  state         TEXT,               -- trial status
  drug_name     TEXT,
  indication    TEXT,
  popular_title TEXT,
  list_page     INTEGER,
  row_index     INTEGER,
  doc_path      TEXT,               -- saved WordML file (relative to out/)
  doc_bytes     INTEGER,
  doc_sha256    TEXT,
  fetched_at    TEXT,
  parsed_at     TEXT,
  fields_json   TEXT,               -- flattened label -> value
  rows_json     TEXT,               -- lossless table rows
  error         TEXT,
  html_path     TEXT,               -- saved detail page (relative to out/)
  html_bytes    INTEGER,
  html_sha256   TEXT,
  html_fetched_at TEXT,
  html_error    TEXT
);
CREATE TABLE IF NOT EXISTS pages (
  page       INTEGER PRIMARY KEY,
  n_ids      INTEGER,
  fetched_at TEXT,
  error      TEXT
);
CREATE INDEX IF NOT EXISTS ix_trials_ctr   ON trials(ctr_no);
CREATE INDEX IF NOT EXISTS ix_trials_doc   ON trials(doc_path);
CREATE INDEX IF NOT EXISTS ix_trials_parse ON trials(parsed_at);
"""

# Columns added after the first harvests ran, so an existing database has to be
# migrated in place rather than recreated - re-downloading is not acceptable.
LATE_COLUMNS = {
    "html_path": "TEXT",
    "html_bytes": "INTEGER",
    "html_sha256": "TEXT",
    "html_fetched_at": "TEXT",
    "html_error": "TEXT",
}


def db(path=DB_FILE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=60, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(trials)")}
    for col, typ in LATE_COLUMNS.items():
        if col not in have:
            con.execute(f"ALTER TABLE trials ADD COLUMN {col} {typ}")
    con.execute("CREATE INDEX IF NOT EXISTS ix_trials_html ON trials(html_path)")
    return con


# ---------- list page parsing ----------
ROW_RE = re.compile(r"<tr[^>]*>\s*<td[^>]*>&nbsp;(\d+)</td>(.*?)</tr>", re.S)
CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
# The row id is whatever getDetail(this.id) is handed. It is a 32-hex GUID that
# MAY carry a suffix - "..._p" is common on older records. An earlier version of
# this pattern required a quote straight after the 32 hex chars and silently
# dropped every suffixed row (~17 of 20 on some pages), so match the attribute.
ID_RE = re.compile(r'onclick="getDetail\(this\.id\)"\s+id="([0-9a-f]{32}[A-Za-z0-9_]*)"')
TAG_RE = re.compile(r"<[^>]+>")
TOTAL_RE = re.compile(r"共\s*<i>(\d+)</i>\s*页，共\s*<i>(\d+)</i>\s*条")

_ENT = {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'"}


def _clean(html):
    t = TAG_RE.sub("", html)
    for k, v in _ENT.items():
        t = t.replace(k, v)
    return re.sub(r"\s+", " ", t).strip()


def parse_list_page(html):
    """-> (rows, total_records, total_pages); rows are dicts ready for `trials`."""
    out = []
    for m in ROW_RE.finditer(html):
        rownum, body = m.group(1), m.group(2)
        cells = CELL_RE.findall(body)
        gid = ID_RE.search(body)
        if not gid or len(cells) < 5:
            continue
        vals = [_clean(c) for c in cells]
        out.append({"id": gid.group(1), "row_index": int(rownum),
                    "ctr_no": vals[0], "state": vals[1], "drug_name": vals[2],
                    "indication": vals[3], "popular_title": vals[4]})
    mt = TOTAL_RE.search(html)
    total_pages = int(mt.group(1)) if mt else None
    total_records = int(mt.group(2)) if mt else None
    return out, total_records, total_pages
