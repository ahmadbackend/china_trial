"""
Phase 0: use nodriver (real Chrome, CDP) to clear the chinadrugtrials.org.cn
JS-challenge WAF (FSSBBIl1Ugzb* / MmEwMD cookies) and dump a session bundle
(cookies + headers + UA) for curl_cffi to reuse.

Usage:  python scraper/bootstrap.py [--headless] [--out out/session.json]
"""
import json, argparse, os, re, sys, time
from pathlib import Path

import nodriver as uc

BASE = "https://www.chinadrugtrials.org.cn"
LIST_URL = f"{BASE}/clinicaltrials.searchlist.dhtml"
WAF_PREFIX = "FSSBBIl1Ugzb"          # WAF cookie family on this host
READY_MARKER = "searchfrm"           # only present once the real page is served


async def grab(headless: bool, out_path: Path, timeout: float = 120.0):
    browser = await uc.start(
        headless=headless,
        browser_args=["--lang=zh-CN,zh", "--disable-blink-features=AutomationControlled"],
    )
    try:
        print("[*] opening index.html ...", flush=True)
        page = await browser.get(f"{BASE}/index.html")

        # The WAF serves HTTP 202 + obfuscated JS which sets the cookies and
        # reloads. NOTE: the genuine pages embed that same WAF script, so the only
        # dependable "we are through" signal is real page content appearing.
        deadline = time.time() + timeout
        while time.time() < deadline:
            await page.sleep(2)
            html = await page.get_content()
            if "clinicaltrials.searchlist" in html and len(html) > 8000:
                break

        # Visit the search list so the WAF also blesses that path and the
        # `token` cookie is minted.
        print("[*] opening search list ...", flush=True)
        page = await browser.get(LIST_URL)
        deadline = time.time() + timeout
        total = None
        while time.time() < deadline:
            await page.sleep(2)
            html = await page.get_content()
            if READY_MARKER in html:
                m = re.search(r"共\s*<i>(\d+)</i>\s*条记录", html)
                if m:
                    total = int(m.group(1))
                break
        else:
            print("[!] search list never settled", file=sys.stderr)

        ua = await page.evaluate("navigator.userAgent")
        raw = await browser.cookies.get_all()

        cookies = {}
        for c in raw:
            dom = (getattr(c, "domain", "") or "").lstrip(".")
            if "chinadrugtrials.org.cn" in dom:
                cookies[c.name] = c.value

        waf = [k for k in cookies if k.startswith(WAF_PREFIX)]
        bundle = {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "user_agent": ua,
            "cookies": cookies,
            "total_records": total,
            "headers": {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                          "image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": BASE,
                "Referer": LIST_URL,
                "User-Agent": ua,
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"[+] cookies: {sorted(cookies)}")
        print(f"[+] WAF cookies present: {waf or 'NONE (!)'} ")
        print(f"[+] total records reported by site: {total}")
        print(f"[+] UA: {ua}")
        print(f"[+] wrote {out_path}")
        return bundle
    finally:
        try:
            browser.stop()
        except Exception:
            pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--out", default="out/session.json")
    a = ap.parse_args()
    out = Path(a.out)
    try:
        uc.loop().run_until_complete(grab(a.headless, out))
    except Exception as e:
        print(f"[!] bootstrap error: {type(e).__name__}: {e}", file=sys.stderr)

    # Judge success by the artefact, not by the event loop: nodriver's teardown
    # on Windows often raises after the bundle has been written correctly.
    ok = False
    if out.exists():
        try:
            b = json.loads(out.read_text(encoding="utf-8"))
            ok = any(k.startswith(WAF_PREFIX) for k in b.get("cookies", {}))
        except Exception:
            ok = False
    sys.stdout.flush()
    sys.stderr.flush()
    # hard-exit so nodriver's atexit handlers cannot flip the status code
    os._exit(0 if ok else 1)
