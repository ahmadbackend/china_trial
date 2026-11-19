"""Shared session / WAF / storage layer for the chinadrugtrials.org.cn harvester."""
import itertools, hashlib, json, re, sqlite3, subprocess, sys, threading, time
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
PROXY_FILE = ROOT / "proxies.txt"

LIST_MARKER = "searchfrm"
DETAIL_MARKER = "一、题目和背景信息"
PAGE_SIZE = 20

BLACKLIST_SECONDS = 600  # 10 minutes


def search_form(page=1, **over):
    d = {"id": "", "ckm_index": "", "sort": "desc", "sort2": "", "rule": "CTR",
         "secondLevel": "0", "currentpage": str(page), "keywords": "", "reg_no": "",
         "indication": "", "case_no": "", "drugs_name": "", "drugs_type": "",
         "appliers": "", "communities": "", "researchers": "", "agencies": "", "state": ""}
    d.update({k: str(v) for k, v in over.items()})
    return d


def load_proxies():
    if not PROXY_FILE.exists():
        return []
    out = []
    for line in PROXY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(":", 3)
        if len(parts) < 4:
            continue
        host = parts[1].lstrip("/")
        port = parts[2]
        user, pwd = parts[3].split(":", 1)
        out.append(f"https://{user}:{pwd}@{host}:{port}")
    return out


def _is_proxy_error(e):
    s = str(e).lower()
    return any(k in s for k in (
        "tunnel failed", "ssl_read", "tls connect",
        "connection closed", "proxy", "402", "412",
        "boringssl", "connect error", "curl: (35)",
        "curl: (56)", "curl: (7)",
    ))


class ChallengeError(RuntimeError):
    pass


class RotateError(ChallengeError):
    pass


class ProxyError(RuntimeError):
    """Proxy connection failed — blacklist this proxy."""
    pass


# ---------------------------------------------------------------------------
# Proxy blacklist: bad proxies cool off for BLACKLIST_SECONDS
# ---------------------------------------------------------------------------
_blacklist = {}  # proxy_url -> expiry timestamp
_bl_lock = threading.Lock()


def _blacklist_proxy(proxy):
    if not proxy:
        return
    with _bl_lock:
        _blacklist[proxy] = time.time() + BLACKLIST_SECONDS
    sid = proxy.rsplit("_session-", 1)[-1] if "_session-" in proxy else "?"
    print(f"[blacklist] proxy session={sid} banned for {BLACKLIST_SECONDS}s",
          flush=True)


def _is_blacklisted(proxy):
    if not proxy:
        return False
    with _bl_lock:
        exp = _blacklist.get(proxy)
        if exp is None:
            return False
        if time.time() > exp:
            del _blacklist[proxy]
            return False
        return True


# ---------------------------------------------------------------------------
# Shared lane: one proxy + one session file, used by many workers
# ---------------------------------------------------------------------------
class _Lane:
    def __init__(self, idx, proxy):
        self.idx = idx
        self.proxy = proxy
        self.px = {"http": proxy, "https": proxy} if proxy else None
        self.session_file = (
            (ROOT / "out" / f"session_{idx}.json") if proxy else SESSION_FILE)
        self.lock = threading.Lock()
        self.stamp = 0.0
        self.req_count = 0


class Session:
    """curl_cffi session backed by a small shared pool of proxy lanes.

    N_LANES proxy+session pairs are bootstrapped (4 browsers, not 20).
    All workers share those lanes round-robin.  Each worker has its own
    curl_cffi Session (thread-safe) but loads cookies from its lane's
    session file.  Proactive rotation happens per-lane after ROTATE_EVERY
    requests across all workers on that lane.
    """

    N_LANES = 4
    ROTATE_EVERY = 200

    _all_proxies = None
    _proxy_counter = None
    _lanes = None
    _lane_counter = None
    _init_lock = threading.Lock()
    _boot_sem = threading.Semaphore(3)

    @classmethod
    def _init_pool(cls):
        with cls._init_lock:
            if cls._lanes is not None:
                return
            cls._all_proxies = load_proxies()
            cls._proxy_counter = itertools.count()
            n = min(cls.N_LANES, len(cls._all_proxies)) if cls._all_proxies else 1
            cls._lanes = []
            for i in range(n):
                proxy = cls._all_proxies[i] if cls._all_proxies else None
                cls._lanes.append(_Lane(i, proxy))
                if cls._all_proxies:
                    next(cls._proxy_counter)
            cls._lane_counter = itertools.count()
            if cls._all_proxies:
                print(f"[pool] {n} lanes, {len(cls._all_proxies)} proxies",
                      flush=True)

    def __init__(self, slot=0, impersonate="chrome"):
        self.impersonate = impersonate
        self._mtime = 0.0
        self.s = None
        self._px = None
        self._init_pool()
        self._lane = self._lanes[next(self._lane_counter) % len(self._lanes)]
        self._load()

    # ---------- session bundle ----------
    def _load(self):
        lane = self._lane
        if not lane.session_file.exists():
            self._bootstrap_lane(lane)
        b = json.loads(lane.session_file.read_text(encoding="utf-8"))
        self.s = cr.Session(impersonate=self.impersonate, headers=b["headers"],
                            cookies=b["cookies"], timeout=(30, 180))
        self._mtime = lane.session_file.stat().st_mtime
        self._px = lane.px

    def _reload_if_stale(self):
        try:
            if self._lane.session_file.stat().st_mtime > self._mtime:
                self._load()
                return True
        except FileNotFoundError:
            pass
        return False

    @classmethod
    def _bootstrap_lane(cls, lane):
        with lane.lock:
            if lane.session_file.exists() and time.time() - lane.stamp < 90:
                return
            with cls._boot_sem:
                cmd = [sys.executable, "-X", "utf8",
                       str(ROOT / "scraper" / "bootstrap.py"),
                       "--out", str(lane.session_file)]
                if lane.proxy:
                    cmd.extend(["--proxy", lane.proxy])
                print(f"[waf] bootstrapping lane {lane.idx} ...", flush=True)
                r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                                   text=True, encoding="utf-8", errors="replace",
                                   timeout=420)
                if r.returncode != 0:
                    raise RuntimeError(
                        f"bootstrap failed:\n{r.stdout}\n{r.stderr}")
                lane.stamp = time.time()
                lane.req_count = 0
                print(f"[waf] lane {lane.idx} ready", flush=True)

    @classmethod
    def _pick_clean_proxy(cls):
        """Pick next proxy that isn't blacklisted."""
        for _ in range(len(cls._all_proxies)):
            idx = next(cls._proxy_counter) % len(cls._all_proxies)
            candidate = cls._all_proxies[idx]
            if not _is_blacklisted(candidate):
                return idx, candidate
        # all blacklisted — pick any and hope
        idx = next(cls._proxy_counter) % len(cls._all_proxies)
        return idx, cls._all_proxies[idx]

    @classmethod
    def _rotate_lane(cls, lane, reason=""):
        if not cls._all_proxies:
            return
        old_proxy = lane.proxy
        with lane.lock:
            idx, proxy = cls._pick_clean_proxy()
            lane.proxy = proxy
            lane.px = {"http": proxy, "https": proxy}
            lane.req_count = 0
        tag = reason or "proactive"
        print(f"[proxy] lane {lane.idx} -> proxy #{idx} ({tag})", flush=True)
        lane.stamp = 0
        cls._bootstrap_lane(lane)

    # ---------- requests ----------
    def post(self, url, data, expect="html", tries=1):
        lane = self._lane

        # proactive rotation
        if self._all_proxies and lane.req_count >= self.ROTATE_EVERY:
            self._rotate_lane(lane, reason="proactive")
            self._load()

        # if our lane's proxy got blacklisted, rotate now
        if _is_blacklisted(lane.proxy):
            self._rotate_lane(lane, reason="blacklisted")
            self._load()

        last = None
        for attempt in range(1, tries + 1):
            try:
                r = self.s.post(url, data=data, proxies=self._px)
                if r.status_code in (403, 429):
                    raise RotateError(f"HTTP {r.status_code}")
                if r.status_code == 202:
                    raise ChallengeError("WAF challenge (HTTP 202)")
                if r.status_code in (401,):
                    raise ChallengeError(
                        f"WAF rejected (HTTP {r.status_code})")
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")

                ctype = r.headers.get("content-type", "")
                if expect == "doc":
                    body = r.content
                    if body.lstrip().startswith(b"<?xml"):
                        if not body.rstrip().endswith(b"</w:wordDocument>"):
                            raise RuntimeError(
                                f"truncated doc ({len(body)} B)")
                        lane.req_count += 1
                        return r
                    if "html" in ctype:
                        raise ChallengeError("got HTML where WordML expected")
                    raise RuntimeError(
                        f"unexpected ctype {ctype} ({len(body)} B)")

                if expect == "html":
                    if LIST_MARKER in r.text:
                        lane.req_count += 1
                        return r
                    raise ChallengeError("missing list marker")

                if expect == "detail":
                    if DETAIL_MARKER in r.text:
                        lane.req_count += 1
                        return r
                    raise ChallengeError("missing detail marker")
                lane.req_count += 1
                return r
            except RotateError as e:
                last = e
                if not self._reload_if_stale():
                    self._rotate_lane(lane, reason=str(e))
                    self._load()
                time.sleep(min(5 * attempt, 30))
            except ChallengeError as e:
                last = e
                if not self._reload_if_stale():
                    self._bootstrap_lane(lane)
                    self._load()
                time.sleep(2 * attempt)
            except Exception as e:
                last = e
                if _is_proxy_error(e):
                    _blacklist_proxy(lane.proxy)
                    self._rotate_lane(lane, reason="proxy error")
                    self._load()
                    time.sleep(min(5 * attempt, 30))
                else:
                    time.sleep(min(3 * attempt, 20))
        raise RuntimeError(f"giving up after {tries} tries: {last}")


def row_sig(row):
    """Fingerprint of the list-row fields; a changed sig means the site edited
    the trial's summary row, so its document should be re-fetched."""
    return hashlib.md5("\x1f".join(
        row.get(k) or "" for k in
        ("state", "drug_name", "indication", "popular_title")
    ).encode("utf-8")).hexdigest()


# ---------- storage ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
  id            TEXT PRIMARY KEY,
  ctr_no        TEXT,
  state         TEXT,
  drug_name     TEXT,
  indication    TEXT,
  popular_title TEXT,
  list_page     INTEGER,
  row_index     INTEGER,
  doc_path      TEXT,
  doc_bytes     INTEGER,
  doc_sha256    TEXT,
  fetched_at    TEXT,
  parsed_at     TEXT,
  fields_json   TEXT,
  rows_json     TEXT,
  error         TEXT,
  html_path     TEXT,
  html_bytes    INTEGER,
  html_sha256   TEXT,
  html_fetched_at TEXT,
  html_error    TEXT,
  row_sig       TEXT,
  needs_refetch INTEGER DEFAULT 0,
  first_seen    TEXT
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

LATE_COLUMNS = {
    "html_path": "TEXT",
    "html_bytes": "INTEGER",
    "html_sha256": "TEXT",
    "html_fetched_at": "TEXT",
    "html_error": "TEXT",
    "row_sig": "TEXT",
    "needs_refetch": "INTEGER DEFAULT 0",
    "first_seen": "TEXT",
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
    con.execute(
        "CREATE INDEX IF NOT EXISTS ix_trials_html ON trials(html_path)")
    return con


# ---------- list page parsing ----------
ROW_RE = re.compile(
    r"<tr[^>]*>\s*<td[^>]*>&nbsp;(\d+)</td>(.*?)</tr>", re.S)
CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
ID_RE = re.compile(
    r'onclick="getDetail\(this\.id\)"\s+id="'
    r'([0-9a-f]{32}[A-Za-z0-9_]*)"')
TAG_RE = re.compile(r"<[^>]+>")
TOTAL_RE = re.compile(
    r"共\s*<i>(\d+)</i>\s*页，共\s*<i>(\d+)</i>\s*条")

_ENT = {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
        "&quot;": '"', "&#39;": "'"}


def _clean(html):
    t = TAG_RE.sub("", html)
    for k, v in _ENT.items():
        t = t.replace(k, v)
    return re.sub(r"\s+", " ", t).strip()


def parse_list_page(html):
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
