"""
Record every network request a real Chrome makes while you browse by hand,
and save each response as a real file you can open locally.

Install:
    pip install seleniumbase
salsabel
Usage:
    python capture_journey.py <url> [options]

A Chrome window (SeleniumBase UC mode) opens on <url>. Click around, log in,
do whatever you need. Recording stops when you press Enter in the terminal,
close the browser, or press Ctrl+C. Prefer Enter: closing the browser loses
the last second of traffic and the cookie dump.

Output folder (default capture_<host>_<time>/):
    html/ js/ css/ json/ images/ fonts/ media/ text/ other/
        <host>/<url path>/<name>.<ext>   one file per response, sorted by
                                         type; JSON is pretty-printed, a
                                         query string goes into the name
    index.json      every request: URL, method, type, request headers (JS-set
                    and on the wire, incl. cookies), POST body, status,
                    response headers, size, timing, redirects, errors,
                    WebSocket frames, and "file" -> its saved response
    capture.har     the same traffic as a HAR. Chrome DevTools -> Network ->
                    Import HAR (the up-arrow icon) shows it like a live session.

Options:
    -o, --out DIR       output folder
    -m, --match TEXT    only keep requests whose URL contains TEXT; repeatable
    --no-assets         skip images, fonts, stylesheets and media
    --no-bodies         record request metadata only, don't save responses
    --no-har            don't write capture.har
    --max-body BYTES    skip saving responses bigger than this (default 50 MB)
    --cf                open with a disconnect/reconnect for Cloudflare-
                        protected sites; requests in the first few seconds
                        are then not recorded
    --profile DIR       reuse a Chrome profile dir, so logins persist

Examples:
    python capture_journey.py https://www.panjabdigilib.org/metadata/<DocID> --cf \\
        -m documentInfo/files -m reader/image
    python capture_journey.py example.com --no-assets

As a function:
    from capture_journey import capture_journey
    result = capture_journey("https://example.com", match=["/api/"])
    for r in result["requests"]:
        print(r["status"], r["url"], r.get("file"))

Only responses from the tab Chrome opened (and tabs it is switched to) can
be saved; requests in other tabs are still listed, with "body_error". The
output contains cookies and auth headers -- treat it like a password.
"""

import argparse
import base64
import hashlib
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlparse

from seleniumbase import SB

ASSET_TYPES = {"Image", "Font", "Stylesheet", "Media"}
NO_BODY_TYPES = {"Preflight", "Ping", "CSPViolationReport", "WebSocket"}
MAX_WS_FRAMES = 5000
MAX_WS_PAYLOAD = 10000

MIME_EXT = {
    "text/html": ".html", "application/xhtml+xml": ".html",
    "text/javascript": ".js", "application/javascript": ".js",
    "application/x-javascript": ".js", "application/ecmascript": ".js",
    "text/css": ".css", "application/json": ".json", "text/plain": ".txt",
    "text/xml": ".xml", "application/xml": ".xml", "text/csv": ".csv",
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "image/avif": ".avif", "image/svg+xml": ".svg",
    "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico",
    "font/woff2": ".woff2", "font/woff": ".woff", "font/ttf": ".ttf",
    "font/otf": ".otf", "application/font-woff": ".woff",
    "video/mp4": ".mp4", "video/webm": ".webm", "audio/mpeg": ".mp3",
    "application/pdf": ".pdf", "application/wasm": ".wasm",
}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _iso(wall_time):
    if not wall_time:
        return None
    return datetime.fromtimestamp(wall_time).astimezone().isoformat(timespec="milliseconds")


def _now():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _category(mime, rtype):
    m = (mime or "").lower()
    if "html" in m or rtype == "Document":
        return "html"
    if "javascript" in m or "ecmascript" in m or rtype == "Script":
        return "js"
    if m == "text/css" or rtype == "Stylesheet":
        return "css"
    if "json" in m:
        return "json"
    if m.startswith("image/") or rtype == "Image":
        return "images"
    if m.startswith("font/") or "woff" in m or rtype == "Font":
        return "fonts"
    if m.startswith(("audio/", "video/")) or rtype == "Media":
        return "media"
    if m.startswith("text/") or "xml" in m:
        return "text"
    return "other"


def _is_text(mime):
    m = (mime or "").lower()
    return m.startswith("text/") or any(t in m for t in ("json", "javascript", "xml", "ecmascript"))


def _segment(s, limit=60):
    s = _BAD_CHARS.sub("_", unquote(s)).strip(" .") or "_"
    return s[:limit]


class _Recorder:
    """Turns the CDP Network events in Chrome's performance log into one
    record per request, saving response bodies under out_dir as they finish.
    The log is drained on every read, so poll() must be called regularly
    while the browser is open."""

    def __init__(self, driver, match=(), skip_assets=False, out_dir=None, max_body=50_000_000):
        self.driver = driver
        self.match = list(match)
        self.skip_assets = skip_assets
        self.out_dir = out_dir      # None: don't save bodies
        self.max_body = max_body
        self.entries = []
        self.by_id = {}        # requestId -> its current entry (latest hop of a redirect chain)
        self.ignored = set()   # requestIds filtered out by match / skip_assets
        self.pending_req = {}  # wire request headers that arrived before requestWillBeSent
        self.pending_resp = {}
        self.used_paths = set()
        self.assets_seen = 0
        self.bodies_saved = 0
        # Don't call Network.enable here (e.g. to raise buffer sizes): after a
        # UC reconnect it stops chromedriver's performance log entirely.

    def _keep(self, url, rtype):
        if url.startswith(("data:", "blob:")):
            return False
        if self.skip_assets and rtype in ASSET_TYPES:
            return False
        return not self.match or any(m in url for m in self.match)

    def _new_entry(self, rid, url, method, rtype, started):
        entry = {
            "url": url,
            "method": method,
            "type": rtype,
            "started": started,
            "status": None,
            "status_text": None,
            "mime_type": None,
            "file": None,
            "request_headers": {},
            "request_headers_wire": None,
            "post_data": None,
            "response_headers": None,
            "response_headers_wire": None,
            "remote_address": None,
            "protocol": None,
            "from_cache": None,
            "size": None,
            "duration_ms": None,
            "error": None,
        }
        wire = self.pending_req.get(rid)
        if wire:
            entry["request_headers_wire"] = wire.pop(0)
        entry["response_headers_wire"] = self.pending_resp.pop(rid, None)
        self.entries.append(entry)
        self.by_id[rid] = entry
        return entry

    def poll(self):
        for raw in self.driver.get_log("performance"):
            try:
                msg = json.loads(raw["message"])["message"]
            except (KeyError, ValueError, TypeError):
                continue
            self._handle(msg.get("method", ""), msg.get("params", {}))

    def _handle(self, method, p):
        rid = p.get("requestId")
        if method == "Network.requestWillBeSent":
            self._on_request(rid, p)
            return
        if method == "Network.webSocketCreated":
            if self._keep(p.get("url", ""), "WebSocket"):
                e = self._new_entry(rid, p.get("url"), "WEBSOCKET", "WebSocket", _now())
                e["frames"] = []
            return
        if rid in self.ignored:
            return
        entry = self.by_id.get(rid)

        if method == "Network.requestWillBeSentExtraInfo":
            headers = p.get("headers", {})
            if entry is not None and entry["request_headers_wire"] is None:
                entry["request_headers_wire"] = headers
            else:
                self.pending_req.setdefault(rid, []).append(headers)
        elif method == "Network.responseReceivedExtraInfo":
            if entry is not None and entry["response_headers_wire"] is None:
                entry["response_headers_wire"] = p.get("headers", {})
            elif entry is None:
                self.pending_resp[rid] = p.get("headers", {})
        elif entry is None:
            return
        elif method == "Network.responseReceived":
            self._apply_response(entry, p.get("response", {}))
            entry["type"] = p.get("type", entry["type"])
        elif method == "Network.loadingFinished":
            entry["size"] = p.get("encodedDataLength")
            if entry.get("_t0") is not None and p.get("timestamp") is not None:
                entry["duration_ms"] = round((p["timestamp"] - entry["_t0"]) * 1000)
            if (self.out_dir is not None and entry["type"] not in NO_BODY_TYPES
                    and entry["status"] not in (204, 304)):
                self._save_body(rid, entry)
            self._announce(entry)
        elif method == "Network.loadingFailed":
            entry["error"] = p.get("errorText")
            if p.get("canceled"):
                entry["error"] = (entry["error"] or "") + " (canceled)"
            if p.get("blockedReason"):
                entry["error"] = (entry["error"] or "") + f" (blocked: {p['blockedReason']})"
            self._announce(entry)
        elif method == "Network.webSocketHandshakeResponseReceived":
            r = p.get("response", {})
            entry["status"] = r.get("status")
            entry["status_text"] = r.get("statusText")
            entry["response_headers"] = r.get("headers")
            entry["request_headers"] = r.get("requestHeaders") or entry["request_headers"]
            self._announce(entry)
        elif method in ("Network.webSocketFrameSent", "Network.webSocketFrameReceived"):
            frames = entry.setdefault("frames", [])
            if len(frames) < MAX_WS_FRAMES:
                r = p.get("response", {})
                payload = r.get("payloadData", "")
                frames.append({
                    "direction": "sent" if method.endswith("Sent") else "received",
                    "time": _now(),
                    "opcode": r.get("opcode"),
                    "payload": payload[:MAX_WS_PAYLOAD],
                    "truncated": len(payload) > MAX_WS_PAYLOAD,
                })
        elif method == "Network.webSocketClosed":
            entry["closed"] = _now()

    def _on_request(self, rid, p):
        req = p.get("request", {})
        url = req.get("url", "")
        prev = self.by_id.get(rid)
        if prev is not None and p.get("redirectResponse"):
            self._apply_response(prev, p["redirectResponse"])
            prev["redirected_to"] = url
            self._announce(prev)

        rtype = p.get("type", "Other")
        if not self._keep(url, rtype):
            self.ignored.add(rid)
            self.by_id.pop(rid, None)
            self.pending_req.pop(rid, None)
            self.pending_resp.pop(rid, None)
            return
        self.ignored.discard(rid)
        entry = self._new_entry(rid, url, req.get("method"), rtype, _iso(p.get("wallTime")))
        entry["request_headers"] = req.get("headers", {})
        entry["post_data"] = req.get("postData")
        entry["initiator"] = p.get("initiator", {}).get("type")
        entry["_t0"] = p.get("timestamp")

    @staticmethod
    def _apply_response(entry, r):
        entry["status"] = r.get("status")
        entry["status_text"] = r.get("statusText")
        entry["response_headers"] = r.get("headers")
        entry["mime_type"] = r.get("mimeType")
        if r.get("remoteIPAddress"):
            entry["remote_address"] = f"{r['remoteIPAddress']}:{r.get('remotePort')}"
        entry["protocol"] = r.get("protocol")
        entry["from_cache"] = bool(
            r.get("fromDiskCache") or r.get("fromPrefetchCache") or r.get("fromServiceWorker")
        )

    def _body_path(self, entry):
        """<category>/<host>/<url path>/<name>[__<query>].<ext>, unique per capture."""
        u = urlparse(entry["url"])
        mime = (entry["mime_type"] or "").lower()
        parts = [p for p in u.path.split("/") if p]
        name = parts.pop() if parts and not u.path.endswith("/") else "index"
        stem, dot, url_ext = name.rpartition(".")
        if not dot or not stem or not url_ext.isalnum() or len(url_ext) > 5:
            stem, url_ext = name, ""
        ext = MIME_EXT.get(mime) or (".json" if mime.endswith("+json") else "")
        ext = ext or (f".{url_ext.lower()}" if url_ext else ".bin")
        stem = _segment(stem, 80)
        if u.query:
            q = _segment(u.query, 50)
            if len(u.query) > 50:
                q += "_" + hashlib.sha1(u.query.encode()).hexdigest()[:8]
            stem += "__" + q
        dirs = [_segment(u.netloc or "local")] + [_segment(p) for p in parts[:8]]
        base = Path(_category(mime, entry["type"]), *dirs, stem + ext)
        rel, n = base, 2
        while str(rel).lower() in self.used_paths:  # Windows paths are case-insensitive
            rel = base.with_name(f"{base.stem}__{n}{base.suffix}")
            n += 1
        self.used_paths.add(str(rel).lower())
        return rel

    def _save_body(self, rid, entry):
        try:
            res = self.driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": rid})
        except Exception as e:  # evicted from Chrome's buffer, other tab, or no body
            entry["body_error"] = str(e).strip().splitlines()[0][:200]
            return
        body = res.get("body", "")
        data = base64.b64decode(body) if res.get("base64Encoded") else body.encode("utf-8")
        if len(data) > self.max_body:
            entry["body_error"] = f"skipped: {len(data)} bytes > --max-body {self.max_body}"
            return
        if "json" in (entry["mime_type"] or ""):
            try:  # pretty-print for reading; the HAR keeps it as-is
                data = json.dumps(json.loads(data), indent=2, ensure_ascii=False).encode("utf-8")
            except ValueError:
                pass
        rel = self._body_path(entry)
        try:
            path = self.out_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as e:  # e.g. a path segment that is already a file
            rel = Path("other", f"{len(self.entries):05d}_{rel.name}")
            path = self.out_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            entry["body_note"] = f"saved flat: {type(e).__name__}"
        entry["file"] = rel.as_posix()
        entry["body_size"] = len(data)
        self.bodies_saved += 1

    def _announce(self, entry):
        if entry["type"] in ASSET_TYPES:
            self.assets_seen += 1
            return
        status = entry["status"] if entry["status"] is not None else (entry["error"] or "ERR")
        print(f"  {str(status):>4} {entry['method'] or '':7} {entry['type'] or '':10} {entry['url'][:140]}")

    def records(self):
        return [{k: v for k, v in e.items() if not k.startswith("_")} for e in self.entries]


def _har_headers(headers):
    out = []
    for name, value in (headers or {}).items():
        for v in str(value).split("\n"):  # Chrome joins repeated headers with \n
            out.append({"name": name, "value": v})
    return out


def _write_har(result, out_dir, path):
    entries = []
    for r in result["requests"]:
        if r["type"] == "WebSocket":
            continue
        u = urlparse(r["url"])
        req_headers = r["request_headers_wire"] or r["request_headers"]
        resp_headers = r["response_headers_wire"] or r["response_headers"]
        content = {"size": r.get("body_size") or 0, "mimeType": r["mime_type"] or ""}
        if r.get("file"):
            data = (out_dir / r["file"]).read_bytes()
            if _is_text(r["mime_type"]):
                content["text"] = data.decode("utf-8", "replace")
            else:
                content["text"] = base64.b64encode(data).decode("ascii")
                content["encoding"] = "base64"
        request = {
            "method": r["method"], "url": r["url"], "httpVersion": r["protocol"] or "",
            "cookies": [], "headers": _har_headers(req_headers),
            "queryString": [{"name": k, "value": v} for k, v in parse_qsl(u.query, keep_blank_values=True)],
            "headersSize": -1, "bodySize": len(r["post_data"] or ""),
        }
        if r["post_data"] is not None:
            ctype = next((v for k, v in (req_headers or {}).items() if k.lower() == "content-type"), "")
            request["postData"] = {"mimeType": ctype, "text": r["post_data"]}
        entries.append({
            "startedDateTime": r["started"] or result["started_at"],
            "time": r["duration_ms"] or 0,
            "request": request,
            "response": {
                "status": r["status"] or 0, "statusText": r["status_text"] or "",
                "httpVersion": r["protocol"] or "", "cookies": [],
                "headers": _har_headers(resp_headers), "content": content,
                "redirectURL": r.get("redirected_to") or "", "headersSize": -1,
                "bodySize": r["size"] if r["size"] is not None else -1,
            },
            "cache": {},
            "timings": {"send": 0, "wait": r["duration_ms"] or 0, "receive": 0},
            "serverIPAddress": (r["remote_address"] or "").rsplit(":", 1)[0],
            "_resourceType": (r["type"] or "other").lower(),
        })
    har = {"log": {"version": "1.2", "creator": {"name": "capture_journey.py", "version": "1"},
                   "pages": [], "entries": entries}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(har, f, ensure_ascii=False)


def _default_out(url):
    host = re.sub(r"[^A-Za-z0-9.-]+", "_", urlparse(url).netloc) or "page"
    return f"capture_{host}_{datetime.now():%Y%m%d-%H%M%S}"


def capture_journey(url, out=None, match=(), skip_assets=False, bodies=True, har=True,
                    max_body=50_000_000, cf=False, profile=None, poll_interval=0.5):
    """Open `url` in Chrome, record network traffic until the user presses
    Enter, closes the browser, or hits Ctrl+C, and return the capture as a
    dict. Writes it to the folder `out` (auto-named when None): one file per
    response, index.json and capture.har. Pass out=False to write nothing
    and only return the request metadata."""
    if "://" not in url:
        url = "https://" + url
    out_dir = None if out is False else Path(out or _default_out(url))
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
    result = {"start_url": url, "final_url": None, "started_at": _now(), "stopped_at": None,
              "stop_reason": None, "requests": [], "cookies": None}
    stop = threading.Event()
    rec = None

    def wait_for_enter():
        try:
            input()
        except (EOFError, OSError):  # no interactive stdin: rely on closing the browser
            return
        stop.set()

    sb_kwargs = {"uc": True, "headless": False, "log_cdp_events": True}
    if profile:
        sb_kwargs["user_data_dir"] = profile

    try:
        with SB(**sb_kwargs) as sb:
            if cf:
                sb.uc_open_with_reconnect(url, reconnect_time=6)
            else:
                # Not sb.open(): in UC mode it can leave chromedriver
                # disconnected, and nothing gets logged while disconnected.
                getattr(sb.driver, "default_get", sb.driver.get)(url)
            if hasattr(sb.driver, "is_connected") and not sb.driver.is_connected():
                sb.reconnect(0.1)
            rec = _Recorder(sb.driver, match, skip_assets,
                            out_dir if bodies else None, max_body)
            if cf:
                # The first load usually happens while chromedriver is
                # disconnected, so none of it was logged. Cloudflare is passed
                # by now; reload once, connected, to record the page itself.
                sb.driver.get_log("performance")
                getattr(sb.driver, "default_get", sb.driver.get)(sb.get_current_url())
                if "just a moment" in (sb.get_title() or "").lower():
                    print("\nCloudflare challenged the reload: solve it in the browser "
                          "window. Recording is already running.")
            print("\nRecording. Browse as much as you like, then press Enter here "
                  "(or close the browser / Ctrl+C) to stop.\n")
            threading.Thread(target=wait_for_enter, daemon=True).start()

            try:
                while not stop.is_set():
                    try:
                        rec.poll()
                        if not sb.driver.window_handles:
                            raise RuntimeError("no windows left")
                    except KeyboardInterrupt:
                        raise
                    except Exception:
                        result["stop_reason"] = "browser closed"
                        break
                    time.sleep(poll_interval)
                else:
                    result["stop_reason"] = "Enter pressed"
            except KeyboardInterrupt:
                result["stop_reason"] = "Ctrl+C"

            if result["stop_reason"] != "browser closed":
                try:
                    rec.poll()
                    result["final_url"] = sb.get_current_url()
                    result["cookies"] = sb.driver.execute_cdp_cmd("Network.getAllCookies", {}).get("cookies")
                except Exception as e:
                    print(f"(could not read final state: {type(e).__name__})")
    except Exception as e:
        if rec is None:  # failed before recording started: a real error
            raise
        # SeleniumBase tearing down a browser the user already closed.
        print(f"(browser shutdown: {type(e).__name__})")

    result["stopped_at"] = _now()
    result["requests"] = rec.records() if rec else []
    result["request_count"] = len(result["requests"])

    n = result["request_count"]
    summary = f"\nStopped ({result['stop_reason']}). Captured {n} request(s)"
    if rec and rec.assets_seen:
        summary += f", including {rec.assets_seen} asset(s) not listed above"
    if out_dir is None:
        print(summary + ".")
        return result

    with open(out_dir / "index.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    if har:
        _write_har(result, out_dir, out_dir / "capture.har")
    print(summary + f"; saved {rec.bodies_saved if rec else 0} response file(s) -> {out_dir}")
    print("  index.json lists every request and its file"
          + ("; capture.har opens in Chrome DevTools -> Network -> Import HAR." if har else "."))
    print("The folder contains cookies and auth headers; don't share it.")
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Record every network request Chrome makes while you browse by hand, "
                    "saving each response as a file.")
    ap.add_argument("url", help="page to open, e.g. https://example.com")
    ap.add_argument("-o", "--out", metavar="DIR", help="output folder (default: capture_<host>_<time>)")
    ap.add_argument("-m", "--match", action="append", default=[], metavar="TEXT",
                    help="only keep requests whose URL contains TEXT (repeatable)")
    ap.add_argument("--no-assets", action="store_true", help="skip images, fonts, stylesheets, media")
    ap.add_argument("--no-bodies", action="store_true", help="don't save response files, metadata only")
    ap.add_argument("--no-har", action="store_true", help="don't write capture.har")
    ap.add_argument("--max-body", type=int, default=50_000_000, metavar="BYTES",
                    help="skip saving responses bigger than this (default 50000000)")
    ap.add_argument("--cf", action="store_true",
                    help="open with reconnect for Cloudflare-protected sites")
    ap.add_argument("--profile", metavar="DIR", help="Chrome profile dir to reuse (keeps logins)")
    a = ap.parse_args(argv)
    capture_journey(a.url, out=a.out, match=a.match, skip_assets=a.no_assets,
                    bodies=not a.no_bodies, har=not a.no_har, max_body=a.max_body,
                    cf=a.cf, profile=a.profile)


if __name__ == "__main__":
    main()
