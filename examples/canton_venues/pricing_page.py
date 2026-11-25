"""The pricing page: the open API stays free, Pro and Data partner sit on top, and a request form.

Open is what the site already gives everyone (the key-free API, MCP, SDK, quotes and checks). Pro
($199 a month) and Data partner (from $3,999 a month) are paid on top; nothing in the open API moves
behind them, and venues cannot buy a place. The form posts to contact.py (``kind: "pricing"``), which
keeps it and forwards it to the owner's Telegram.

The page changes only when this file does, so deploy writes it once:

    python pricing_page.py --out /var/www/canton-venues
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from venue_pages import SITE, _shell, _write, e

PLANS = ("pro", "partner")  # the values the form sends; contact.py accepts only these

CHECK = ('<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.4 6.6 11.5 12.5 4.8" fill="none" '
         'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>')

# One column per tier: name, price, who it is for, the action, then what it adds to the one before.
TIERS = [
    {"id": "open", "name": "Open", "from": False, "price": "$0", "per": "",
     "desc": "For anyone building on Canton market data.",
     "cta": ("quiet", "/build/", "Start with one call", ""),
     "plus": "Included",
     "items": ["JSON API, every five minutes", "No key, no sign-up", "MCP server and open‑source SDK",
               "Quotes and trade checks at any size", "Eight days of history"]},
    {"id": "pro", "name": "Pro", "from": False, "price": "$199", "per": "a month",
     "desc": "For research and backtests on Canton market history.",
     "cta": ("primary", "#request", "Request Pro", "pro"),
     "plus": "Everything in Open, plus",
     "items": ["Archive since 27 October 2026", "Updates every minute", "Push alerts by webhook or Telegram",
               "Higher rate limits"]},
    {"id": "partner", "name": "Data partner", "from": True, "price": "$3,999", "per": "a month",
     "desc": "For data companies, oracles and desks.",
     "cta": ("primary", "#request", "Talk to us", "partner"),
     "plus": "Everything in Pro, plus",
     "items": ["Delivery in your format", "Written methodology", "Incident notices",
               "Monthly data‑quality report", "Custom pairs and venues", "First month as a pilot"]},
]

PAGE_CSS = """
.ptitle { padding: 44px 0 36px; }
.ptitle h1 { font-size: 40px; font-weight: 700; letter-spacing: -0.02em; line-height: 1.1; margin: 0; }
.ptitle p { font-size: 16px; color: var(--text-2); margin: 12px 0 0; text-wrap: balance; }
.tiers { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); grid-template-rows: auto auto auto auto 1fr;
         border: 1px solid var(--line-2); border-radius: 16px; background: var(--card); }
.tier { display: grid; grid-row: span 5; grid-template-rows: subgrid; padding: 32px 32px 36px; min-width: 0; scroll-margin-top: 80px; }
.tier + .tier { border-left: 1px solid var(--line-2); }
.tier h2 { font-size: 15px; font-weight: 600; margin: 0; letter-spacing: 0; }
.tprice { display: flex; align-items: baseline; gap: 8px; margin: 16px 0 0; white-space: nowrap; }
.tprice b { font-size: 48px; font-weight: 600; letter-spacing: -0.03em; line-height: 1; }
.tprice span { font-size: 14px; color: var(--text-2); font-weight: 500; }
.tdesc { font-size: 15px; color: var(--text-2); margin: 14px 0 0; text-wrap: pretty; }
.tcta { margin-top: 24px; }
.tcta a { display: block; text-align: center; border-radius: 10px; padding: 11px 16px; font-weight: 600; font-size: 14px; border: 1px solid transparent; }
.tcta a:hover { text-decoration: none; }
/* a fixed blue, not --accent: white text keeps 4.5:1 in the dark theme too */
.tcta .primary, .reqbox .form button { background: #3861fb; color: #fff; }
.tcta .primary:hover { filter: brightness(1.08); }
.tcta .quiet { background: var(--bg-2); color: var(--text); border-color: var(--line-2); }
.tcta .quiet:hover { border-color: var(--text-3); }
.tlist { margin-top: 28px; padding-top: 26px; border-top: 1px solid var(--line); }
.tlist p { margin: 0 0 14px; font-size: 13px; font-weight: 600; color: var(--text-2); }
.tlist ul { list-style: none; margin: 0; padding: 0; font-size: 14px; }
.tlist li { display: flex; gap: 10px; align-items: flex-start; margin: 0 0 12px; }
.tlist li:last-child { margin-bottom: 0; }
.tlist svg { flex: none; width: 16px; height: 16px; margin-top: 2px; color: var(--text-2); }
.tnote { text-align: center; color: var(--text-2); font-size: 13px; margin: 18px auto 0; max-width: 90ch; text-wrap: balance; }
.reqbox { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 2fr); border: 1px solid var(--line-2);
          border-radius: 16px; background: var(--card); margin-top: 64px; scroll-margin-top: 80px; }
.reqtext { padding: 32px; border-right: 1px solid var(--line-2); }
.reqtext h2 { font-size: 20px; font-weight: 700; margin: 0; letter-spacing: -0.01em; }
.reqtext p { color: var(--text-2); font-size: 15px; margin: 10px 0 0; text-wrap: pretty; }
.reqbox .form { padding: 32px; }
.form fieldset { border: 0; padding: 0; margin: 0; display: grid; gap: 6px; }
.form legend { font-size: 13px; font-weight: 600; padding: 0; margin-bottom: 6px; }
.plans-pick { display: flex; gap: 10px 22px; flex-wrap: wrap; }
.plans-pick label { display: flex; align-items: center; gap: 8px; font-weight: 500; font-size: 14px; }
.plans-pick input { width: auto; margin: 0; }
.plans-pick span { color: var(--text-2); }
@media (max-width: 900px) {
  .ptitle { padding: 24px 0; }
  .ptitle h1 { font-size: 32px; }
  .tiers { grid-template-columns: minmax(0, 1fr); grid-template-rows: none; }
  .tier { display: block; grid-row: auto; padding: 24px; }
  .tier + .tier { border-left: 0; border-top: 1px solid var(--line-2); }
  .tlist { margin-top: 22px; padding-top: 20px; }
  .reqbox { grid-template-columns: minmax(0, 1fr); margin-top: 40px; }
  .reqtext { border-right: 0; border-bottom: 1px solid var(--line-2); padding: 24px; }
  .reqbox .form { padding: 24px; }
}
"""

FORM_JS = """
(function () {
  var f = document.getElementById("reqform"), st = document.getElementById("reqst");
  document.querySelectorAll("a[data-plan]").forEach(function (a) {
    a.addEventListener("click", function (ev) {
      ev.preventDefault();
      var r = f.querySelector('input[name=plan][value="' + a.dataset.plan + '"]');
      if (r) r.checked = true;
      document.getElementById("request").scrollIntoView({ behavior: "smooth", block: "start" });
      f.app.focus({ preventScroll: true });
      try { window.umami && window.umami.track("pricing-cta", { plan: a.dataset.plan }); } catch (err) {}
    });
  });
  f.addEventListener("submit", async function (ev) {
    ev.preventDefault();
    var btn = f.querySelector("button[type=submit]");
    var plan = (f.querySelector("input[name=plan]:checked") || {}).value || "";
    var body = { kind: "pricing", plan: plan, app: f.app.value, contact: f.contact.value, message: f.message.value, website: f.website.value };
    if (!body.contact.trim() || body.app.trim().length < 2) { st.className = "st err"; st.textContent = "Add your name or company and a way to reach you."; return; }
    btn.disabled = true; st.className = "st"; st.textContent = "Sending…";
    try {
      var r = await fetch("/api/contact", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (r.status === 429) throw new Error("Too many messages from here, try again in a minute.");
      if (!r.ok) throw new Error("Could not send. Try again, or reach us in the Telegram channel.");
      f.reset(); st.className = "st ok"; st.textContent = "Sent. We will reply within a day.";
      try { window.umami && window.umami.track("pricing-sent", { plan: plan }); } catch (err) {}
    } catch (err) { st.className = "st err"; st.textContent = err.message; }
    btn.disabled = false;
  });
})();
"""


def tier(t: dict) -> str:
    kind, href, label, plan = t["cta"]
    data = f' data-plan="{plan}"' if plan else ""
    frm = "<span>from</span>" if t["from"] else ""
    per = f'<span>{e(t["per"])}</span>' if t["per"] else ""
    items = "".join(f"<li>{CHECK}<span>{e(i)}</span></li>" for i in t["items"])
    return f"""    <div class="tier" id="plan-{e(t['id'])}">
      <h2>{e(t['name'])}</h2>
      <div class="tprice">{frm}<b>{e(t['price'])}</b>{per}</div>
      <p class="tdesc">{e(t['desc'])}</p>
      <div class="tcta"><a class="{kind}" href="{e(href)}"{data}>{e(label)}</a></div>
      <div class="tlist"><p>{e(t['plus'])}</p><ul>{items}</ul></div>
    </div>"""


def page(t: int) -> str:
    tiers = "\n".join(tier(x) for x in TIERS)
    body = f"""  <p class="crumbs"><a href="/">Canton Venues</a> / Pricing</p>
  <div class="ptitle"><h1>Pricing</h1><p>The open API is free and stays free. Pay when you need the data faster, further back, or with a contract behind&nbsp;it.</p></div>
  <div class="tiers">
{tiers}
  </div>
  <p class="tnote">Pay monthly in USD, CC or USDCx, stop any month. Venues cannot buy a place: the rankings, this site and the open API are the same for everyone, paying or&nbsp;not.</p>

  <section class="reqbox" id="request">
    <div class="reqtext">
      <h2>Request access</h2>
      <p>Say which plan and what you run. We reply within a day with payment&nbsp;details.</p>
    </div>
    <form class="form" id="reqform" novalidate>
      <fieldset><legend>Plan</legend>
        <div class="plans-pick">
          <label><input type="radio" name="plan" value="pro" checked> Pro <span>$199 a month</span></label>
          <label><input type="radio" name="plan" value="partner"> Data partner <span>from $3,999 a month</span></label>
        </div>
      </fieldset>
      <div class="two">
        <label>Name or company <input name="app" required maxlength="120"></label>
        <label>Email or Telegram <input name="contact" required maxlength="200"></label>
      </div>
      <label>What you run and what you need <textarea name="message" maxlength="4000" style="min-height:90px"></textarea></label>
      <label class="hp" aria-hidden="true">Website <input name="website" tabindex="-1" autocomplete="off"></label>
      <div class="send"><button type="submit">Send request</button><span class="st" id="reqst" role="status"></span></div>
    </form>
  </section>"""
    html = _shell("Pricing: Canton market data, free and paid | Canton Venues",
                  "The open Canton Venues API stays free. Pro: the archive, updates every minute and push alerts, "
                  "$199 a month. Data partner: delivery in your format with a methodology and incident notices, "
                  "from $3,999 a month.",
                  f"{SITE}/pricing/", None, body, t, FORM_JS)
    # the shared footer says every number "on this page" is in the API; prices are not
    html = html.replace("Every number on this page is in the open JSON API", "Every market number on this site is in the open JSON API", 1)
    return html.replace("</style>", PAGE_CSS + "</style>", 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True, help="the web root; writes <out>/pricing/index.html")
    args = ap.parse_args()
    _write(args.out / "pricing" / "index.html", page(int(datetime.now(UTC).timestamp())))


if __name__ == "__main__":
    main()
