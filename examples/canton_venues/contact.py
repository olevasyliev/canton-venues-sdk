"""Canton Venues contact form: one POST endpoint, no address on the page.

Each message is appended to a JSON-lines file and, when ``CONTACT_CHAT_ID`` is set,
forwarded by the site's Telegram bot to that chat. The file is the record; Telegram
is the notification, so a failed send loses nothing.

Two forms post here: the dashboard's contact form (a message) and the build page's
"Register your app" (``kind: "register"``: the app, what it uses, whether it wants a
listing and its own line for it). A listing goes up by hand (build_page.APPS), never
straight from the form.

    python contact.py --port 8098 --log /opt/canton-venues/contact.jsonl

nginx proxies ``POST /api/contact`` here and rate-limits it per IP.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("contact")

MAX_BODY = 16_000
LIMITS = {"name": 120, "contact": 200, "message": 4000, "app": 120, "url": 300, "uses": 200, "line": 280}


def clean(form: dict, key: str) -> str:
    return str(form.get(key) or "").strip()[: LIMITS[key]]


def read_form(form: dict) -> tuple[dict | None, str]:
    """The entry to keep, or None and why not. A form without ``kind`` is the contact form."""
    if form.get("kind") == "register":
        entry = {"kind": "register", "app": clean(form, "app"), "url": clean(form, "url"),
                 "uses": clean(form, "uses"), "contact": clean(form, "contact"),
                 "message": clean(form, "message"), "listed": form.get("listed") in (True, "on", "yes"),
                 "line": clean(form, "line")}
        if not entry["contact"] or len(entry["app"]) < 2:
            return None, "app and contact are required"
        return entry, ""
    entry = {"name": clean(form, "name"), "contact": clean(form, "contact"), "message": clean(form, "message")}
    if not entry["contact"] or len(entry["message"]) < 2:
        return None, "contact and message are required"
    return entry, ""


def notify_text(entry: dict) -> str:
    """The Telegram message for one entry, every field escaped for Telegram's HTML."""
    q = html.escape
    if entry.get("kind") == "register":
        listing = (f"yes, line: {q(entry['line'])}" if entry["line"] else "yes, no line") if entry["listed"] else "no"
        return (f"<b>cantonvenues.com: app registered</b>\n"
                f"App: {q(entry['app'])}\n"
                f"URL: {q(entry['url'] or '-')}\n"
                f"Uses: {q(entry['uses'] or '-')}\n"
                f"Reply to: {q(entry['contact'])}\n"
                f"Listing: {listing}\n\n"
                f"{q(entry['message'] or '(nothing on what they build)')}")
    return (f"<b>cantonvenues.com: new message</b>\n"
            f"From: {q(entry['name'] or '-')}\n"
            f"Reply to: {q(entry['contact'])}\n\n"
            f"{q(entry['message'])}")


def notify(entry: dict) -> None:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("CONTACT_CHAT_ID")
    if not (token and chat):
        return
    text = notify_text(entry)
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        # the URL carries the bot token: never log it
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=10) as r:
            if r.status != 200:
                log.warning("telegram: HTTP %s", r.status)
    except Exception as exc:  # noqa: BLE001 - the message is already on disk
        log.warning("telegram: %s", type(exc).__name__)


class Handler(BaseHTTPRequestHandler):
    log_path: Path

    def _reply(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 < size <= MAX_BODY:
            return self._reply(413 if size else 400, {"ok": False, "error": "empty or too large"})
        try:
            form = json.loads(self.rfile.read(size))
            assert isinstance(form, dict)
        except Exception:  # noqa: BLE001
            return self._reply(400, {"ok": False, "error": "bad request"})
        if form.get("website"):  # honeypot: people never see this field
            return self._reply(200, {"ok": True})
        entry, why = read_form(form)
        if entry is None:
            return self._reply(422, {"ok": False, "error": why})
        entry = {"t": int(time.time()), **entry, "ip": self.headers.get("X-Real-IP", "")}
        with self.log_path.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        notify(entry)
        self._reply(200, {"ok": True})

    def log_message(self, fmt, *args) -> None:  # nginx already logs every request
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Canton Venues contact form")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8098)
    ap.add_argument("--log", default="contact.jsonl")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    Handler.log_path = Path(args.log)
    os.umask(0o077)  # messages carry people's contacts: owner-only file
    if Handler.log_path.exists():
        Handler.log_path.chmod(0o600)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
