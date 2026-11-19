"""Proxy pool for the Scotia module — sticky per session, rotate on block.

Wraps the simple count-based ProxyRotator into what this module actually
needs: a Scotia session is bound to the IP it was earned on, so SeleniumBase
(login) and curl_cffi (API replay) MUST share one proxy, and that proxy must
NOT change mid-session. Rotation only happens when an IP is genuinely blocked
(Akamai ban page / 403 on GET calls) or dead — never on a request count.

Proxy file: one proxy per line, `host:port:username:password` (Decodo format,
residential or datacenter endpoints both work). Default location:
`proxies.txt` next to this file (gitignored); override with SCOTIA_PROXIES_FILE.
"""

from __future__ import annotations

import logging
import os
import random
import time
from pathlib import Path

log = logging.getLogger("scotia.proxy")

DEFAULT_PROXY_FILE = Path(__file__).resolve().parent / "proxies_decodo.txt"

HEALTHCHECK_URL = "https://api.ipify.org?format=json"
HEALTHCHECK_TIMEOUT = 8


class ProxyRotator:
    """Loads Decodo-style proxies (`host:port:username:password` per line).

    The proxy is returned as a string compatible with curl_cffi:
        https://username:password@host:port
    """

    def __init__(self, proxy_file):
        self.proxies = self._load_proxies(proxy_file)
        if not self.proxies:
            raise RuntimeError(f"No proxies loaded from {proxy_file}")
        self.current_index = random.randrange(len(self.proxies))
        log.info("loaded %d proxies; first: %s", len(self.proxies), self._mask(self.current()))

    @staticmethod
    def _load_proxies(path):
        proxies = []
        if not os.path.exists(path):
            raise FileNotFoundError(f"Proxy file not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("http://") or line.startswith("https://"):
                    proxies.append(line)
                    continue
                parts = line.split(":")
                if len(parts) != 4:
                    log.warning("skipping malformed proxy line: %s", line)
                    continue
                host, port, username, password = parts
                proxies.append(f"https://{username}:{password}@{host}:{port}")
        return proxies

    @staticmethod
    def _mask(proxy_url):
        """Hide credentials when logging a proxy URL."""
        if not proxy_url:
            return "DIRECT"
        at = proxy_url.rfind("@")
        return f"https://***:***@{proxy_url[at + 1:]}" if at != -1 else "***"

    def current(self):
        return self.proxies[self.current_index]

    def rotate(self) -> str:
        if len(self.proxies) < 2:
            return self.current()
        old = self.current_index
        while self.current_index == old:
            self.current_index = random.randrange(len(self.proxies))
        log.info("rotated to %s", self._mask(self.current()))
        return self.current()


class StickyProxyPool:
    """Holds one proxy for a whole session; rotates only on block/death.

    Mode "recovery": callers start DIRECT (no proxy); the pool is only asked
    for a proxy when an IP block is hit. `bind(url)` re-attaches a session to
    the proxy it was earned with (recorded in session_data.har).
    """

    def __init__(self, proxy_file=None):
        path = proxy_file or os.environ.get("SCOTIA_PROXIES_FILE") or DEFAULT_PROXY_FILE
        self.rotator = ProxyRotator(path)
        self._current: str | None = None
        self._failed: dict[str, float] = {}  # proxy url -> last failure ts

    # -- selection -----------------------------------------------------------

    @staticmethod
    def mask(url):
        return ProxyRotator._mask(url)

    def bind(self, url: str | None) -> str | None:
        """Re-attach to the proxy recorded for an existing session.

        A session recorded WITHOUT a proxy (None) must NOT wipe a rotation
        selected for the next login — bind only reattaches real proxies."""
        if url:
            self._current = url
        return self._current

    def current(self) -> str | None:
        return self._current

    def _pick_fresh(self) -> str:
        now = time.time()
        candidates = [
            p for p in self.rotator.proxies
            if p != self._current and now - self._failed.get(p, 0) > 3600
        ]
        if not candidates:  # everything failed recently — reset and take any other
            log.warning("all proxies failed within the hour; resetting failure marks")
            self._failed.clear()
            candidates = [p for p in self.rotator.proxies if p != self._current] or list(self.rotator.proxies)
        pick = random.choice(candidates)
        self.rotator.current_index = self.rotator.proxies.index(pick)
        return pick

    def mark_blocked(self, url: str | None = None) -> None:
        target = url or self._current
        if target:
            self._failed[target] = time.time()
            log.warning("proxy marked blocked: %s", ProxyRotator._mask(target))

    def rotate(self, reason: str = "") -> str:
        """Switch to a fresh proxy and return it (caller re-logins through it)."""
        self.mark_blocked(self._current)
        self._current = self._pick_fresh()
        log.warning("rotated to %s (%s)", ProxyRotator._mask(self._current), reason or "requested")
        return self._current

    # -- health ----------------------------------------------------------------

    @staticmethod
    def healthcheck(proxy_url: str, timeout: int = HEALTHCHECK_TIMEOUT, attempts: int = 3) -> bool:
        """Cheap liveness check — run BEFORE a browser login so a dead proxy
        never wastes an OTP SMS. Retried: datacenter gateways are flaky
        (CONNECT tunnel 400 comes and goes), so one failure ≠ dead."""
        import curl_cffi.requests

        for attempt in range(1, attempts + 1):
            try:
                r = curl_cffi.requests.get(
                    HEALTHCHECK_URL,
                    proxies={"http": proxy_url, "https": proxy_url},
                    timeout=timeout,
                )
                if r.status_code == 200:
                    log.info("healthcheck %s -> HTTP 200", ProxyRotator._mask(proxy_url))
                    return True
                log.warning("healthcheck %s -> HTTP %s (attempt %d/%d)",
                            ProxyRotator._mask(proxy_url), r.status_code, attempt, attempts)
            except Exception as exc:
                log.warning("healthcheck %s failed (attempt %d/%d): %s",
                            ProxyRotator._mask(proxy_url), attempt, attempts, exc)
            if attempt < attempts:
                time.sleep(1.5)
        return False

    def rotate_until_healthy(self, reason: str = "", attempts: int = 5) -> str:
        for _ in range(attempts):
            proxy = self.rotate(reason)
            if self.healthcheck(proxy):
                return proxy
            log.warning("unhealthy proxy, trying another")
        raise RuntimeError(f"no healthy proxy after {attempts} attempts")


def load_pool(proxy_file=None) -> StickyProxyPool | None:
    """None when no proxy file exists (direct mode is the default)."""
    path = Path(proxy_file or os.environ.get("SCOTIA_PROXIES_FILE") or DEFAULT_PROXY_FILE)
    if not path.exists():
        return None
    return StickyProxyPool(path)
