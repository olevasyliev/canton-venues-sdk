"""The "Build on Canton Venues" page: one call to start with for each kind of app, a form to register
an app, and the apps that asked to be listed, in their own words.

The page changes only when this file does, so deploy writes it once:

    python build_page.py --out /var/www/canton-venues

Registering is optional and changes nothing in the API: no key, the same files for everyone. It gets a
team notice before a breaking change, direct help and, if it wants one, a listing. The form posts to
contact.py (``kind: "register"``); a listing goes up by hand, in ``APPS``, after a look at the app.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from venue_pages import SITE, _shell, _write, e

API = f"{SITE}/api/v1"
NOTICE_DAYS = 14

# Teams that registered and asked to be listed: name, url, their own line (never written by us), and
# since (the day they registered, YYYY-MM-DD). Checked by hand before a row goes up.
APPS: list[dict] = []

# one panel per kind of app: who it is for, what the call answers, the call, and a JS and a Python line
STARTS = [
    {
        "id": "wallet",
        "who": "A wallet or a swap screen",
        "what": "The best venue for this swap",
        "about": "The same trade priced on every venue by its best route on its own markets, one swap or two: "
                 "pool and book fees, price impact and each venue's network fee included. "
                 "<code>best_venue</code> is the one that returns the most after its network fee.",
        "url": f"{API}/quote?sell=CC&buy=USDCx&amount_usd=1000",
        "js": 'const url = API + "/quote?sell=CC&buy=USDCx&amount_usd=1000";\n'
              'const q = await fetch(url).then((r) => r.json());\n'
              'q.best_venue;  // the venue to route to\n'
              'q.by_venue[q.best_venue].after_network_fee;  // USDCx out',
        "py": 'import requests\n'
              'q = requests.get(f"{API}/quote", params={\n'
              '    "sell": "CC", "buy": "USDCx", "amount_usd": 1000}).json()\n'
              'best = q["by_venue"][q["best_venue"]]\n'
              'q["best_venue"], best["after_network_fee"]',
        "note": "Any size: <code>amount</code> in the token you sell, or <code>amount_usd</code>. Computed on request, "
                "30 a minute per IP.",
    },
    {
        "id": "router",
        "who": "A router, an aggregator or a DCA product",
        "what": "Your fill against the best venue at its time",
        "about": "Give a trade you made and its time. It is priced again on every venue from the market as we read "
                 "it at the five-minute readings either side, and <code>shortfall_bps</code> says how far your fill "
                 "fell short of the best venue. Nothing is executed or stored.",
        "url": f"{API}/check?sell=CC&buy=USDCx&amount_in=10000&amount_out=985&time=2026-10-28T07:05:00Z&venue=cantex",
        "js": 'const iso = "2026-10-28T07:05:00Z";  // when it filled\n'
              'const url = API + "/check?sell=CC&buy=USDCx&amount_in=10000"\n'
              '  + "&amount_out=985&venue=cantex&time=" + iso;\n'
              'const c = await fetch(url).then((r) => r.json());\n'
              'c.best_venue, c.shortfall_bps;  // your gap in bp',
        "py": 'c = requests.get(f"{API}/check", params={\n'
              '    "sell": "CC", "buy": "USDCx", "amount_in": 10000,\n'
              '    "amount_out": 985, "venue": "cantex",\n'
              '    "time": "2026-10-28T07:05:00Z"}).json()\n'
              'c["best_venue"], c["shortfall_bps"]',
        "note": "<code>time</code> as ISO (2026-10-28T07:05:00Z) or unix seconds; the record starts 27 Oct 2026.",
    },
    {
        "id": "risk",
        "who": "Lending, vaults and risk",
        "what": "What collateral sells for on Canton",
        "about": "For every token: what selling it for USDCx nets as a share of its value, from $1K to $1M, per "
                 "venue and at the best one; the largest sale each venue and all of them together take within 1%, "
                 "2% and 5%, and what limited it (the cost, or the end of a visible order book).",
        "url": f"{API}/exit.json",
        "js": 'const x = await fetch(API + "/exit.json").then((r) => r.json());\n'
              'const cbtc = x.tokens.find((t) => t.symbol === "CBTC");\n'
              'cbtc.capacity_usd["2pct"].all_venues;  // $ within 2%',
        "py": 'x = requests.get(f"{API}/exit.json").json()\n'
              'cbtc = next(t for t in x["tokens"] if t["symbol"] == "CBTC")\n'
              'cbtc["capacity_usd"]["2pct"]["all_venues"]',
        "note": "Refreshed every five minutes; <code>worst</code> keeps the lowest share at $10K and $50K since "
                "<code>worst_since</code>.",
    },
    {
        "id": "analytics",
        "who": "Analytics and research",
        "what": "Volume and history you can check",
        "about": "Every venue's daily spot volume for the last 30 closed UTC days with each day's records side by "
                 "side (the venue's own, our midnight reading of its 24-hour counter, an outside record) and "
                 "whether they agree within 5%. Eight days of prices and premiums in five-minute steps in "
                 "<code>history.json</code>.",
        "url": f"{API}/volume.json",
        "js": 'const v = await fetch(API + "/volume.json").then((r) => r.json());',
        "py": 'v = requests.get(f"{API}/volume.json").json()',
        "note": 'Every file and field: <a href="/api/v1/">the endpoint list</a> and <a href="/llms.txt">llms.txt</a>.',
    },
]

PAGE_CSS = """
.starts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.starts .panel { scroll-margin-top: 80px; }
.who { color: var(--text-2); font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; margin: 0 0 6px; }
.starts .sub, .regwhy li { font-size: 13.5px; }
.starts pre.curl { margin-top: 10px; white-space: pre; }
.starts .open { display: inline-block; margin-top: 10px; font-weight: 600; font-size: 13px; }
.starts .lang { color: var(--text-3); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; margin: 12px 0 -4px; }
.starts .fine { margin-top: 10px; }
code { font: 12.5px ui-monospace, Menlo, monospace; background: var(--bg-2); border-radius: 5px; padding: 1px 5px; }
.reg { display: grid; grid-template-columns: 1fr 1.3fr; gap: 32px; align-items: start; }
.regwhy { margin: 6px 0 0; padding-left: 18px; color: var(--text-2); }
.regwhy li { margin: 8px 0; }
.regwhy b { color: var(--text); }
.form [hidden] { display: none; }
.form .check { display: flex; align-items: center; gap: 8px; font-weight: 500; }
.form .check input { width: auto; margin: 0; }
.form .uses { display: flex; gap: 6px 18px; flex-wrap: wrap; }
.form fieldset { border: 0; padding: 0; margin: 0; display: grid; gap: 6px; }
.form legend { font-size: 13px; font-weight: 600; padding: 0; margin-bottom: 6px; }
.apps { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
.apps .panel p { margin: 6px 0 0; color: var(--text-2); font-size: 13.5px; }
@media (max-width: 900px) { .starts, .reg, .apps { grid-template-columns: minmax(0, 1fr); } }
"""

FORM_JS = """
(function () {
  var f = document.getElementById("regform"), st = document.getElementById("regst"), line = document.getElementById("regline");
  f.listed.addEventListener("change", function () { line.hidden = !f.listed.checked; });
  f.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    var btn = f.querySelector("button[type=submit]");
    var uses = Array.prototype.map.call(f.querySelectorAll("input[name=use]:checked"), function (x) { return x.value; });
    var body = { kind: "register", app: f.app.value, url: f.url.value, uses: uses.join(", "), contact: f.contact.value,
                 message: f.message.value, listed: f.listed.checked, line: f.listed.checked ? f.line.value : "", website: f.website.value };
    if (!body.contact.trim() || body.app.trim().length < 2) { st.className = "st err"; st.textContent = "Add the app's name and a way to reach you."; return; }
    btn.disabled = true; st.className = "st"; st.textContent = "Sending…";
    try {
      var r = await fetch("/api/contact", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (r.status === 429) throw new Error("Too many messages from here, try again in a minute.");
      if (!r.ok) throw new Error("Could not send. Try again, or reach us in the Telegram channel.");
      f.reset(); line.hidden = true; st.className = "st ok"; st.textContent = "Registered. Thank you, we will be in touch.";
      try { window.umami && window.umami.track("register-sent"); } catch (err) {}
    } catch (err) { st.className = "st err"; st.textContent = err.message; }
    btn.disabled = false;
  });
})();
"""


def start_panel(s: dict) -> str:
    return f"""    <div class="panel" id="{e(s["id"])}">
      <p class="who">{e(s["who"])}</p>
      <h3>{e(s["what"])}</h3>
      <p class="sub">{s["about"]}</p>
      <pre class="curl">GET {e(s["url"])}</pre>
      <a class="open" href="{e(s["url"])}" target="_blank" rel="noopener">Open the live answer</a>
      <p class="lang">JavaScript</p>
      <pre class="curl">{e(s["js"])}</pre>
      <p class="lang">Python</p>
      <pre class="curl">{e(s["py"])}</pre>
      <p class="fine">{s["note"]}</p>
    </div>"""


def apps_html() -> str:
    """The listed apps, or nothing while none is: the section appears with the first."""
    if not APPS:
        return ""
    cards = "".join(f'<div class="panel"><h3><a href="{e(a["url"])}" target="_blank" rel="noopener">{e(a["name"])}</a></h3>'
                    f'<p>{e(a["line"])}</p><p class="fine">Since {e(a["since"])}</p></div>' for a in APPS)
    return f"""
  <section class="block" id="built">
    <h2>Built on Canton Venues</h2>
    <p class="sub">Apps that use the data and asked to be listed, each in its own words.</p>
    <div class="apps">{cards}</div>
  </section>"""


def page(t: int) -> str:
    starts = "\n".join(start_panel(s) for s in STARTS)
    body = f"""  <p class="crumbs"><a href="/">Canton Venues</a> / Build</p>
  <div class="title" style="padding-top:4px"><h1>Build on Canton Venues</h1><p>Live market data for every Canton trading venue, for your wallet, router, dashboard or agent: a JSON API, an MCP server and an <a href="https://github.com/olevasyliev/canton-venues-sdk">open-source SDK</a>. No key, no sign-up, cross-origin enabled, refreshed every five minutes. The API, the MCP server, the history and the code stay open and key-free.</p></div>
{apps_html()}
  <section class="block" id="start">
    <h2>Start with one call</h2>
    <p class="sub">Pick the one that matches what you build. Each answer is plain JSON over GET; open it in a browser to see it live. In the code, <code>API</code> is <code>https://cantonvenues.com/api/v1</code>.</p>
    <div class="starts">
{starts}
    </div>
    <div class="grid2" style="margin-top:16px">
      <div class="panel" id="agents">
        <p class="who">An AI agent</p>
        <h3>The same data over MCP</h3>
        <p class="sub">Add it in Claude as a custom connector, or in any MCP client as a streamable HTTP server. Read-only, no key. Tools: market_overview, list_tokens, token, quote, premiums, spreads, alerts, pools, venues, perps, exit_cost, check_trade.</p>
        <pre class="curl">https://cantonvenues.com/mcp</pre>
      </div>
      <div class="panel" id="venue">
        <p class="who">A Canton venue</p>
        <h3>Be read from your own feed</h3>
        <p class="sub">We read public APIs only. A public quote endpoint lets us check our math against your own engine every hour, as we do for Tradecraft, Cantex and OneSwap (<a href="/api/v1/quotecheck.json">quotecheck.json</a>). A venue is an adapter in the open-source SDK; a pull request that adds yours is welcome, or <a href="#register">register</a> and point us to your docs.</p>
      </div>
    </div>
  </section>

  <section class="block" id="register">
    <div class="reg">
      <div>
        <h2 style="font-size:20px;margin:0">Register your app</h2>
        <ul class="regwhy">
          <li><b>Notice before a breaking change,</b> at least {NOTICE_DAYS} days ahead, to the contact you give. A new field never breaks anything; removing or renaming a field or a file, or changing what one means, does.</li>
          <li><b>Direct help.</b> A question about a field, a route or a number gets a reply within a day.</li>
          <li><b>What you need next.</b> Ask for a pair, a field or a view; tell us what breaks for you.</li>
          <li><b>A listing on this page</b> if you want one, in your own words.</li>
        </ul>
        <p class="fine">Registering is optional: the API is the same with it or without it. Your contact is used only to reach you about the data.</p>
      </div>
      <form class="panel form" id="regform" novalidate>
        <div class="two">
          <label>App or team <input name="app" required maxlength="120"></label>
          <label>Website or repo <input name="url" maxlength="300" placeholder="https://"></label>
        </div>
        <fieldset><legend>What you use</legend>
          <div class="uses">
            <label class="check"><input type="checkbox" name="use" value="JSON files"> JSON files</label>
            <label class="check"><input type="checkbox" name="use" value="quote or check"> Quote or check</label>
            <label class="check"><input type="checkbox" name="use" value="MCP"> MCP</label>
            <label class="check"><input type="checkbox" name="use" value="SDK"> SDK</label>
          </div>
        </fieldset>
        <label>Email or Telegram <input name="contact" required maxlength="200" placeholder="For notices and replies"></label>
        <label>What you build with it <textarea name="message" maxlength="4000" style="min-height:90px"></textarea></label>
        <label class="check"><input type="checkbox" name="listed"> List us on this page</label>
        <label id="regline" hidden>Your line for the listing <input name="line" maxlength="280" placeholder="In your own words: what you use the data for"></label>
        <label class="hp" aria-hidden="true">Website <input name="website" tabindex="-1" autocomplete="off"></label>
        <div class="send"><button type="submit">Register</button><span class="st" id="regst" role="status"></span></div>
      </form>
    </div>
  </section>"""
    html = _shell("Build on Canton Venues: free Canton market data for your app | Canton Venues",
                  "Best venue for a swap, your fill against every venue, what collateral sells for, and checked "
                  "volume, for every Canton trading venue. JSON API, MCP and an open-source SDK, no key. Register "
                  f"your app for {NOTICE_DAYS} days' notice before a breaking change and direct help.",
                  f"{SITE}/build/", None, body, t, FORM_JS)
    return html.replace("</style>", PAGE_CSS + "</style>", 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True, help="the web root; writes <out>/build/index.html")
    args = ap.parse_args()
    _write(args.out / "build" / "index.html", page(int(datetime.now(UTC).timestamp())))


if __name__ == "__main__":
    main()
