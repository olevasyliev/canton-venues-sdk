"""The privacy and terms pages, in the site's shell. Directories that list the MCP server (Claude's
connectors, ChatGPT's) ask for both, and the privacy page must say what is recorded, why, who sees
it and for how long. Every line here describes what the code does: the MCP call log (mcp_server.py,
no arguments kept), the forms (contact.py, forwarded to Telegram), the web server's log, Umami,
the theme kept in local storage.

The pages change only when this file does, so deploy writes them once:

    python legal_pages.py --out /var/www/canton-venues
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from venue_pages import SITE, _shell, _write

UPDATED = "24 November 2026"

PAGE_CSS = """
.legal { max-width: 760px; padding-bottom: 24px; }
.legal h1 { font-size: 32px; font-weight: 700; letter-spacing: -0.02em; margin: 28px 0 6px; }
.legal .upd { color: var(--text-3); font-size: 13px; margin: 0 0 22px; }
.legal h2 { font-size: 18px; font-weight: 700; margin: 30px 0 8px; }
.legal p, .legal li { color: var(--text-2); font-size: 15px; line-height: 1.6; text-wrap: pretty; }
.legal p b, .legal li b { color: var(--text); font-weight: 600; }
.legal ul { padding-left: 20px; margin: 8px 0; }
.legal li { margin: 6px 0; }
"""

PRIVACY = f"""  <p class="crumbs"><a href="/">Canton Venues</a> / Privacy</p>
  <article class="legal">
    <h1>Privacy</h1>
    <p class="upd">Updated {UPDATED}</p>
    <p>Canton Venues is run by Oleksii Vasyliev. It shows public market data and asks for nothing to use it: no account, no key, no cookies. This page says what is recorded when you use the site, the API, the MCP server or a form, why, who sees it and for how long.</p>

    <h2>What is recorded</h2>
    <ul>
      <li><b>Every request to the site, the API and the MCP server</b> is written to the web server's log: the IP address, the time, the address requested, the browser or client name and the referring page. It is used to run and protect the service (rate limits, errors, abuse) and to count distinct users in aggregate.</li>
      <li><b>Each MCP tool call</b> is also recorded on its own: the tool's name, the name and version of the client software, the IP address, the time and whether the call succeeded. The arguments of the call and the question you asked your assistant are not recorded. It is used to see which tools are used and to report usage in aggregate, for example to the Canton Foundation's Development Fund, which funds this project in part per verified adoption.</li>
      <li><b>Forms</b> (contact, app registration, pricing request) keep what you type in them, plus the IP address and the time, to answer you and to keep out spam.</li>
      <li><b>Page visits</b> are counted with Umami, a cookie-free analytics service: the page, the referring site, the country and device type, and clicks on outbound links, reported in aggregate.</li>
      <li><b>Your browser</b> keeps your light or dark theme choice in its local storage. It never leaves your device.</li>
    </ul>

    <h2>Who sees it</h2>
    <p>Only the operator of Canton Venues. Form messages are forwarded to him through Telegram's Bot API, so Telegram processes them. Umami processes the visit counts. The pages load fonts from Google Fonts and some token logos from CoinGecko, so those services see your IP address when your browser fetches them. Aggregate figures (requests, distinct users, tool calls) may be published; IP addresses and form contents never are. Nothing is sold, and nothing is used for advertising or tracking across other sites.</p>

    <h2>How long</h2>
    <p>Web server logs and the MCP call log are deleted within 12 months at most. Form messages are kept for up to 12 months after the last exchange, or deleted sooner when you ask.</p>

    <h2>Your choices</h2>
    <p>You can ask what is held about you, or ask for it to be deleted, through the <a href="/#contact">contact form</a>. You will get an answer within 30 days. Changes to this page are dated at the top.</p>
  </article>"""

TERMS = f"""  <p class="crumbs"><a href="/">Canton Venues</a> / Terms</p>
  <article class="legal">
    <h1>Terms of use</h1>
    <p class="upd">Updated {UPDATED}</p>
    <p>Canton Venues is run by Oleksii Vasyliev. Using the site, the API or the MCP server means you accept these terms.</p>

    <h2>What this is</h2>
    <p>Information about trading venues on the Canton Network, computed from their public data. It is not investment advice, not an offer to buy or sell anything, and not a broker. Nothing here signs or executes a trade.</p>

    <h2>Accuracy</h2>
    <p>Figures are computed automatically and can be wrong, late or incomplete; each page states its method and sources. Check a price on the venue itself before you trade. The service is provided as is, without warranty, and to the extent the law allows, its operator is not liable for losses that come from using it.</p>

    <h2>Using the data</h2>
    <p>The open API, the MCP server and the site are free to use for any purpose, including in commercial products. Please credit Canton Venues (cantonvenues.com) where you show the data. Keep within the limits (computed answers such as quotes and trade checks: 30 a minute per IP address), and don't try to get around them or overload the service. Paid plans (<a href="/pricing/">pricing</a>) are governed by the agreement for each plan; they never take anything away from the free API.</p>

    <h2>Venues</h2>
    <p>Venue names and marks belong to their owners. A venue listed here has not necessarily endorsed Canton Venues, and no venue can pay for its place in the rankings.</p>

    <h2>Changes and contact</h2>
    <p>These terms may change; the date at the top says when they last did. Questions go through the <a href="/#contact">contact form</a>.</p>
  </article>"""


def page(kind: str, t: int) -> str:
    if kind == "privacy":
        html = _shell("Privacy | Canton Venues", "What Canton Venues records when you use the site, the API, the MCP "
                      "server or a form, why, who sees it and for how long.", f"{SITE}/privacy/", None, PRIVACY, t)
    else:
        html = _shell("Terms of use | Canton Venues", "Terms for using Canton Venues: public market data for Canton "
                      "trading venues, not investment advice, free to use with credit.", f"{SITE}/terms/", None, TERMS, t)
    # the shared footer says every number "on this page" is in the API; these pages carry none
    html = html.replace("Every number on this page is in the open JSON API", "Every market number on this site is in the open JSON API", 1)
    return html.replace("</style>", PAGE_CSS + "</style>", 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True, help="the web root; writes <out>/privacy/ and <out>/terms/")
    args = ap.parse_args()
    t = int(datetime.now(UTC).timestamp())
    for kind in ("privacy", "terms"):
        _write(args.out / kind / "index.html", page(kind, t))


if __name__ == "__main__":
    main()
