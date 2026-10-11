"""Authenticated HTTP helpers shared by the Slack bot and local workers."""

from __future__ import annotations

import json
import ipaddress
import os
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def validate_url(base_url: str) -> None:
    url = urlsplit(base_url)
    host = (url.hostname or "").lower()
    if url.scheme not in {"http", "https"} or not host or url.username or url.password:
        raise RuntimeError("Coordinator URL must be an HTTP or HTTPS URL without embedded credentials")
    if url.scheme == "https" or host in {"localhost", "127.0.0.1", "::1"}:
        return
    # Explicit opt-in for a literal LAN IP; do not trust hostname substrings.
    try:
        address = ipaddress.ip_address(host)
        private_lan = any(address in ipaddress.ip_network(network) for network in
                          ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))
    except ValueError:
        private_lan = False
    if private_lan and os.getenv("MEETPIPE_ALLOW_INSECURE_HTTP") == "1":
        return
    raise RuntimeError("Remote coordinator URLs must use HTTPS; for a private LAN IP, set MEETPIPE_ALLOW_INSECURE_HTTP=1")


def request_json(base_url: str, token: str, method: str, path: str, payload: dict | None = None,
                 timeout: int = 75) -> dict:
    if not base_url or not token:
        raise RuntimeError("Set MEETPIPE_COORDINATOR_URL and the appropriate coordinator token")
    validate_url(base_url)
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(base_url.rstrip("/") + path, data=data, method=method,
                  headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                           **({"Content-Type": "application/json"} if data is not None else {})})
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.loads(response.read())
    except HTTPError as e:
        try:
            detail = e.read(2000).decode(errors="replace")
        finally:
            e.close()
        raise RuntimeError(f"Coordinator returned HTTP {e.code}: {detail}") from e
    except URLError as e:
        raise RuntimeError(f"Could not reach coordinator: {e.reason}") from e
