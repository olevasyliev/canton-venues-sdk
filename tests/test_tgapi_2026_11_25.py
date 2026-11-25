"""tgapi: every Bot API call binds an IPv4 source address (the server's IPv6 route to Telegram stalls)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import contact  # noqa: E402
import tgapi  # noqa: E402


class FakeConn:
    made: list = []

    def __init__(self, host, timeout=None, source_address=None):
        self.args = {"host": host, "timeout": timeout, "source_address": source_address}
        FakeConn.made.append(self)

    def request(self, verb, path, body, headers):
        self.req = (verb, path, body, headers)

    def getresponse(self):
        class R:
            status = 200

            def read(self):
                return b'{"ok":true}'
        return R()

    def close(self):
        self.closed = True


def test_a_call_goes_out_over_ipv4_to_the_bot_api(monkeypatch):
    FakeConn.made = []
    monkeypatch.setattr(tgapi.http.client, "HTTPSConnection", FakeConn)
    assert tgapi.call("t0k3n", "sendMessage", {"chat_id": "42", "text": "a & b"}, timeout=7) == 200
    c, = FakeConn.made
    assert c.args == {"host": "api.telegram.org", "timeout": 7, "source_address": ("0.0.0.0", 0)}
    verb, path, body, headers = c.req
    assert (verb, path) == ("POST", "/bott0k3n/sendMessage") and body == b"chat_id=42&text=a+%26+b"
    assert headers["Content-Type"] == "application/x-www-form-urlencoded" and c.closed


def test_the_contact_form_forwards_through_tgapi(monkeypatch):
    sent = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t0k3n")
    monkeypatch.setenv("CONTACT_CHAT_ID", "42")
    monkeypatch.setattr(contact.tgapi, "call", lambda tok, m, f, timeout=15: sent.append((tok, m, f["chat_id"])) or 200)
    contact.notify({"name": "N", "contact": "@n", "message": "hello"})
    assert sent == [("t0k3n", "sendMessage", "42")]
