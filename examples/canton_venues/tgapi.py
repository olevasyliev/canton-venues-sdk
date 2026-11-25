"""The Telegram Bot API over IPv4 only.

From the server, api.telegram.org's IPv6 address does not answer (25.11.2026: ``curl -6`` timed out at 25 s while
google.com and api.cantex.io answered over IPv6 in milliseconds). urllib and synchronous httpx try that address
first and wait out their whole timeout before the IPv4 one: a contact-form message held its visitor ~10 s, an alert
~15 s, a manual channel preview ~10 s per post. Binding an IPv4 source address makes the IPv6 attempt fail at once
(0.09 s per call, measured on the server). The collector's async client is not affected: anyio races the two
addresses and answered in 0.4 s.
"""
from __future__ import annotations

import http.client
import urllib.parse

HOST = "api.telegram.org"
IPV4_ANY = ("0.0.0.0", 0)


def call(token: str, method: str, fields: dict, timeout: float = 15) -> int:
    """POST one Bot API method (sendMessage, sendPhoto, ...) and return the HTTP status.
    The request path carries the token: callers never log the path, only the status or the error's type."""
    conn = http.client.HTTPSConnection(HOST, timeout=timeout, source_address=IPV4_ANY)
    try:
        conn.request("POST", f"/bot{token}/{method}", urllib.parse.urlencode(fields).encode(),
                     {"Content-Type": "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()
