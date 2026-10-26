"""Offline tests for the weekly card: "Canton trading this week"."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import crosscheck as cx
import publish_guard as pg
import weekly as wk

DAY = 86400
END = 1791331200          # 2026-10-07 00:00 UTC
NOW = END + 20 * 60       # the 00:20 run
START = END - 7 * DAY     # 2026-09-30
LLAMA = {"temple": 30e6, "cantex": 4e6, "rocky": 3e6, "pool-party": 4000.0}


FINAL = END + 7 * 3600     # a read of DefiLlama's day record six hours and more after the day closed


def hist(days: int = 14, read: int = FINAL, skip: dict | None = None, extra_total: float = 0.0) -> dict:
    """venue_history.json with DefiLlama daily points for the last ``days`` days before END, each
    venue's figure growing by 1% a day; ``skip`` drops a venue's points on some days."""
    skip = skip or {}
    daily, total = {}, {}
    for n in range(days):
        t = END - (days - n) * DAY
        for slug, base in LLAMA.items():
            if t in skip.get(slug, ()):
                continue
            v = round(base * (1 + 0.01 * n), 2)
            daily.setdefault(slug, {"source": "defillama", "t": read, "points": []})["points"].append([t, v])
            total[t] = total.get(t, 0.0) + v
    tc = {"source": "tradecraft", "t": read, "points": [[END - k * DAY, 200_000.0] for k in range(6, 0, -1)]}
    daily["tradecraft"] = tc
    return {"daily": daily, "daily_total": [[t, v + extra_total] for t, v in sorted(total.items())],
            "hourly": {"t": [], "venues": {}}, "t": read - 120}


def test_the_period_is_the_seven_finished_utc_days():
    assert wk.period(NOW) == (START, END)
    assert wk.period(END) == (START, END)  # a run at 00:00 exactly: the day before is the last one
    assert wk.date_range(START, END) == "30 Sep to 6 Oct 2026"
    assert wk.date_range(END - 6 * DAY + 0, END + DAY) == "1 to 7 Oct 2026"


def test_total_shares_scope_and_week_on_week_from_full_data():
    w = wk.week(hist(), NOW)
    llama_total = sum(sum(LLAMA[s] * (1 + 0.01 * n) for s in LLAMA) for n in range(7, 14))
    assert abs(w["total"] - llama_total) < 1
    assert [v["slug"] for v in w["venues"]] == ["temple", "cantex", "rocky", "pool-party"]
    assert abs(sum(v["share"] for v in w["venues"]) - 1) < 1e-9
    assert w["scope"] == "canton" and wk.label(w) == "Canton DEX spot volume"
    # every venue has the seven days before too: a change, from the same venues
    prev = sum(sum(LLAMA[s] * (1 + 0.01 * n) for s in LLAMA) for n in range(0, 7))
    assert abs(w["change"] - (w["total"] / prev - 1)) < 1e-9 and wk.change_text(w).startswith("+")
    # Tradecraft has six days: left out, named, never filled in
    assert [x["slug"] for x in w["left_out"]] == ["tradecraft"] and "30 Sep" in w["left_out"][0]["why"]


def test_a_missing_day_or_a_zero_leaves_the_venue_out_and_drops_the_canton_claim():
    w = wk.week(hist(skip={"rocky": {START + 2 * DAY}}), NOW)
    assert "rocky" not in [v["slug"] for v in w["venues"]]
    assert any(x["slug"] == "rocky" and "2 Oct" in x["why"] for x in w["left_out"])
    assert w["scope"] == "read" and wk.label(w) == "Spot volume, Canton venues we read"
    h = hist()
    h["daily"]["cantex"]["points"][-1][1] = 0  # a source that lags stores 0: a missing day, not a zero
    assert "cantex" not in [v["slug"] for v in wk.week(h, NOW)["venues"]]


def test_an_outside_day_equal_to_the_day_before_is_no_figure():
    # DefiLlama repeated Rocky's 18 Oct 2026 figure for 19 Oct while Rocky's API was down
    h = hist()
    pts = h["daily"]["rocky"]["points"]
    pts[-5][1] = pts[-6][1]
    w = wk.week(h, NOW)
    assert "rocky" not in [v["slug"] for v in w["venues"]]
    assert any(x["slug"] == "rocky" and "2 Oct" in x["why"] for x in w["left_out"])
    # the venue's own record of the same day stands, and so does a venue's own series that repeats
    figs, _ = wk.day_figures(h, {"rocky": {str(START + 2 * DAY): {"usd": 1.0}}}, "rocky")
    assert figs[START + 2 * DAY] == (1.0, "rocky")
    h["daily"]["tradecraft"]["points"].insert(0, [START, 200_000.0])
    assert "tradecraft" in [v["slug"] for v in wk.week(h, NOW)["venues"]]


def test_no_week_on_week_when_the_week_before_is_not_complete():
    # Rocky absent from DefiLlama for three days the week before (as on 27-29 Sep 2026)
    w = wk.week(hist(skip={"rocky": {START - 3 * DAY, START - 2 * DAY, START - DAY}}), NOW)
    assert w["change"] is None and w["prev_total"] is None and "rocky" in [v["slug"] for v in w["venues"]]
    assert any("No week-on-week change" in n for n in wk.notes(w))


def test_a_protocol_we_do_not_know_on_the_canton_list_drops_the_canton_claim():
    assert wk.week(hist(extra_total=5_000.0), NOW)["scope"] == "read"


def test_a_daily_record_read_before_the_day_closed_is_not_counted():
    # read at 23:59: the last day's figure was DefiLlama's running total, so it is no figure at all
    w = wk.week(hist(read=END - 60), NOW)
    assert w is None or not w["venues"]
    # read at 00:15: its point for the closed day may still be a pre-midnight rolling 24 h figure.
    # The venues wait (pending), they are not left out, and nothing is published
    h = hist(read=END + 15 * 60)
    figs, early = wk.day_figures(h, {}, "temple")
    assert early == {END - DAY} and END - DAY not in figs and END - 2 * DAY in figs  # only the day just closed
    h["daily"]["tradecraft"]["points"].insert(0, [START, 200_000.0])  # one venue with a final week
    w = wk.week(h, NOW)
    assert {x["slug"] for x in w["pending"]} == set(LLAMA) and not w["left_out"]
    assert all(x["days"] == [END - DAY] for x in w["pending"])
    # read seven hours after the close: everything up to the closed day is final
    assert not wk.day_figures(hist(), {}, "temple")[1]


def test_the_venues_own_day_records_win_and_cantex_needs_our_price_for_the_day():
    h = hist()
    days = [START + n * DAY for n in range(7)]
    cache = {"temple": {str(t): {"usd": 31e6, "t": NOW} for t in days},
             "cantex": {str(t): {"cc": 40e6, "t": NOW} for t in days}}
    # our CC price readings: every 10 minutes on all but the first day, $0.12
    ts = [t for t in range(START + DAY, END, 600)]
    prices = {"t": ts, "usd": {"CC": [0.12] * len(ts)}}
    wk.fetch_days(cache, prices, NOW, get=lambda *a: (_ for _ in ()).throw(AssertionError("no fetch: final")))
    assert "usd" not in cache["cantex"][str(START)] and abs(cache["cantex"][str(START + DAY)]["usd"] - 4.8e6) < 1
    w = wk.week(h, NOW, cache)
    temple = next(v for v in w["venues"] if v["slug"] == "temple")
    assert temple["src"] == ["temple"] * 7 and temple["total"] == 7 * 31e6
    cantex = next(v for v in w["venues"] if v["slug"] == "cantex")
    assert cantex["src"] == ["defillama"] + ["cantex"] * 6  # no price record for 30 Sep: DefiLlama's day
    note = wk.venue_note(cantex, w["days"])
    assert "an outside daily record of the closed day (small print) for 30 Sep" in note and "at our CC price" in note
    assert "DefiLlama" not in note
    assert wk.venue_note(temple, w["days"]) == "Temple: Temple's own settled volume, each UTC day."


def test_our_midnight_reading_stands_for_the_day_that_closed():
    h = hist()
    ts = [START + n * DAY + 25 * 60 for n in range(1, 8)] + [START + 3 * DAY + 5 * 3600]
    h["hourly"] = {"t": ts, "venues": {"rocky": {"spot_volume": [2.5e6] * 7 + [9e9]}}}
    rocky = next(v for v in wk.week(h, NOW)["venues"] if v["slug"] == "rocky")
    assert rocky["src"] == ["reading"] * 7 and rocky["total"] == 7 * 2.5e6  # the 05:00 reading is not one


def test_fetch_days_reads_each_venue_day_once_with_a_24_hour_window_and_fails_alone():
    calls = []

    def get(url, params):
        calls.append((url, params))
        if url == cx.TEMPLE:
            if params["start_time"].startswith("2026-10-04"):
                raise OSError("502")
            return {"total_volume_usd": 36e6}
        return {"data": {"volume_cc": "1000"}}

    cache = wk.fetch_days({}, {}, NOW, get)
    t = [c for c in calls if c[0] == cx.TEMPLE]
    assert {p["start_time"][11:] for _, p in t} == {"00:00:01Z"} and {p["end_time"][11:] for _, p in t} == {"00:00:00Z"}
    assert len(cache["temple"]) == 13 and str(END - 3 * DAY) not in cache["temple"]  # 4 Oct failed twice
    assert len(cache["cantex"]) == 14 and all("usd" not in r for r in cache["cantex"].values())
    # read again at 07:00: only the days not yet final (read under six hours after they closed)
    calls.clear()
    wk.fetch_days(cache, {}, END + 7 * 3600, get)
    assert {p["start_time"][:10] for _, p in calls} == {"2026-10-06", "2026-10-04"}


def _built(tmp_path, h=None, now=NOW, guard=None):
    return wk.build(tmp_path, h or hist(), now, guard)


def test_build_writes_the_latest_page_a_permalink_the_card_and_the_api(tmp_path):
    from PIL import Image

    w = _built(tmp_path)
    card = tmp_path / "weekly" / "2026-10-06" / "card.png"
    assert Image.open(card).size == (1200, 630)
    latest = (tmp_path / "weekly" / "index.html").read_text()
    perma = (tmp_path / "weekly" / "2026-10-06" / "index.html").read_text()
    for page in (latest, perma):
        assert '<meta name="twitter:card" content="summary_large_image">' in page
        assert f'content="https://cantonvenues.com/weekly/2026-10-06/card.png?v={NOW}"' in page
    assert '<link rel="canonical" href="https://cantonvenues.com/weekly/">' in latest
    assert '<link rel="canonical" href="https://cantonvenues.com/weekly/2026-10-06/">' in perma
    # Share on X posts the permalink, so the post keeps this week's card
    href = latest.split('class="btn x" href="')[1].split('"')[0].replace("&amp;", "&")
    q = parse_qs(urlparse(href).query)
    assert q["url"] == ["https://cantonvenues.com/weekly/2026-10-06/"]
    # the card, the headline and the share text never credit an outside aggregator
    head = latest.split("</head>")[0]
    assert "DefiLlama" not in head and "DefiLlama" not in q["text"][0]
    api = json.loads((tmp_path / "api" / "v1" / "weekly.json").read_text())
    assert api["total_usd"] == round(w["total"], 2) and api["slug"] == "2026-10-06"
    assert api["venues"][0]["daily_source"] == ["defillama"] * 7 and len(api["by_day"]) == 7


def test_the_card_draws_no_source_but_ours():
    src = Path(wk.__file__).read_text()
    card = src.split("def render_card")[1].split("# === page")[0]
    assert "DefiLlama" not in card and "defillama" not in card and '"cantonvenues.com"' in card


def test_later_weeks_link_earlier_permalinks_and_never_rewrite_them(tmp_path):
    _built(tmp_path)
    first = (tmp_path / "weekly" / "2026-10-06" / "index.html").read_text()
    h = hist(days=15, read=FINAL + DAY)
    for s in h["daily"].values():
        if s["source"] == "defillama":
            s["points"] = [[t + DAY, v] for t, v in s["points"]]
    h["daily_total"] = [[t + DAY, v] for t, v in h["daily_total"]]
    _built(tmp_path, h, NOW + DAY)
    assert (tmp_path / "weekly" / "2026-10-06" / "index.html").read_text() == first
    latest = (tmp_path / "weekly" / "index.html").read_text()
    assert "1 to 7 Oct 2026" in latest and 'href="/weekly/2026-10-06/"' in latest


def test_the_guard_holds_the_last_good_card_on_a_jump_or_a_stopped_collector(tmp_path):
    g = pg.PublishGuard(tmp_path / "weekly" / "published.json")
    assert _built(tmp_path, guard=g) is not None
    h = hist()
    for s in h["daily"].values():
        s["points"] = [[t, v * 5] for t, v in s["points"]]
    h["daily_total"] = [[t, v * 5] for t, v in h["daily_total"]]
    assert _built(tmp_path, h, NOW + 6 * 3600, g) is None  # 5x in a day: held
    # the collector stopped: its last read of a daily record is three hours old
    assert _built(tmp_path, hist(), FINAL + 3 * 3600, pg.PublishGuard(tmp_path / "weekly" / "published.json")) is None


def test_freshness_is_the_last_daily_read_not_the_hourly_sample(tmp_path):
    g = pg.PublishGuard(tmp_path / "weekly" / "published.json")
    assert _built(tmp_path, hist(), FINAL + 5 * 60, g) is not None
    h = hist()
    h["t"] = FINAL - 55 * 60  # the last hourly sample is 55 minutes old, over the 40-minute limit ...
    assert _built(tmp_path, h, FINAL + 5 * 60, g) is not None  # ... but the daily records were read now


def test_the_0020_run_publishes_nothing_until_defillama_days_are_final(tmp_path):
    assert _built(tmp_path, hist(read=END + 15 * 60), END + 20 * 60) is None
    assert not (tmp_path / "weekly").exists()
    assert _built(tmp_path, hist(read=END + 6 * 3600 + 5 * 60), END + 6 * 3600 + 20 * 60) is not None


def test_tradecraft_joins_once_it_has_all_seven_days():
    # the 9 Oct run: 2 to 8 Oct, Tradecraft's own days read after 8 Oct closed
    now = END + 2 * DAY + 7 * 3600
    h = hist(days=16, read=now - 60)
    for s in h["daily"].values():
        if s["source"] == "defillama":
            s["points"] = [[t + 2 * DAY, v] for t, v in s["points"]]
    h["daily_total"] = [[t + 2 * DAY, v] for t, v in h["daily_total"]]
    h["daily"]["tradecraft"]["points"] = [[END - DAY * k, 200_000.0] for k in range(6, -2, -1)]
    w = wk.week(h, now)
    assert w["range"] == "2 to 8 Oct 2026" and "tradecraft" in [v["slug"] for v in w["venues"]]
    assert not w["left_out"]


def test_shown_shares_add_up_to_100_and_day_figures_keep_one_decimal():
    w = wk.week(hist(), NOW)
    for v, share in zip(w["venues"], (0.79261, 0.12849, 0.07877, 0.00013)):
        v["share"] = share
    shown = wk.shares(w)
    assert shown == ["79.3%", "12.8%", "7.9%", "<0.1%"]
    assert round(sum(float(x[:-1]) for x in shown if x != "<0.1%"), 1) == 100.0
    assert wk.usd(28_014_286) == "$28.0M" and wk.usd(45_723_736) == "$45.7M" and wk.usd(35_852) == "$35.9K"


def test_the_page_names_defillama_once_in_small_print_with_its_own_method_and_the_nav_links_it(tmp_path):
    _built(tmp_path)
    page = (tmp_path / "weekly" / "index.html").read_text()
    body = page.split("</head>")[1]
    assert body.count("DefiLlama") == 1 and '<p class="fine">' in body.split("DefiLlama")[0][-400:]
    assert wk.WEEKLY_METHOD in page and wk.SHELL_METHOD not in page
    assert '<a href="/weekly/">This week</a>' in page
    api = json.loads((tmp_path / "api" / "v1" / "weekly.json").read_text())
    assert not any("DefiLlama" in n for n in api["notes"]) and "DefiLlama" in api["small_print"]
    dash = (Path(wk.__file__).parent / "site" / "index.html").read_text()
    assert '<a href="/weekly/">This week</a>' in dash


def test_crosscheck_compares_the_weekly_sums_and_reports_a_stale_or_missing_week(monkeypatch):
    weekly = wk._api(wk.week(hist(), NOW), NOW)
    llama = {"totalDataChartBreakdown": [[t, {"Temple": LLAMA["temple"] * (1 + 0.01 * n),
                                              "Cantex": LLAMA["cantex"] * (1 + 0.01 * n)}]
                                         for n, t in enumerate(range(END - 14 * DAY, END, DAY))]}
    temple_calls = []

    def get(url, params):
        if url == cx.LLAMA_DEXS:
            return llama
        if url == cx.TEMPLE:
            temple_calls.append(params)
            t = int(cx.datetime.fromisoformat(params["start_time"].replace("Z", "+00:00")).timestamp()) - 1
            return {"total_volume_usd": LLAMA["temple"] * (1 + 0.01 * (7 + (t - START) // DAY))}
        raise AssertionError(url)

    figs = cx.weekly_figures(get, weekly)
    assert set(figs) == {("weekly", "volume:temple"), ("weekly", "volume:cantex"), ("weekly", "volume:temple:own")}
    assert len(temple_calls) == 7  # Temple refuses windows over 24 hours: one call a day
    rows = cx.compare(cx.weekly_published(weekly), figs, cx.GAP)
    assert len(rows) == 3 and all(abs(r["gap"]) < 1e-9 for r in rows)

    monkeypatch.setattr(cx, "venue_figures", lambda get, now: {})
    monkeypatch.setattr(cx, "llama_figures", lambda get: {})
    fresh = {"venues": {"temple": {"t": END + 3 * DAY, "figures": {}}}}
    when = cx.datetime.fromtimestamp(END + 3 * DAY, cx.UTC)
    text, _ = cx.run(fresh, get, when, weekly)
    assert "Weekly card not regenerated: its week ended 3.0 days ago." in text
    text, _ = cx.run(fresh, get, when, {})
    assert "No weekly card published" in text
    assert cx.run(fresh, get, cx.datetime.fromtimestamp(END + 3600, cx.UTC), weekly)[0] is None


def test_the_collector_stamps_when_it_read_each_daily_record():
    import venue_history as vh

    state = {}
    vh.set_daily(state, "temple", "defillama", [[START, 1.0]], NOW)
    assert state["daily"]["temple"] == {"source": "defillama", "t": NOW, "points": [[START, 1.0]]}
