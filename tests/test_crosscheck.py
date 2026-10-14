"""The daily cross-check, with every outside fetch mocked."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import crosscheck as cx

NOW = datetime(2026, 10, 7, 7, 0, tzinfo=UTC)
CC = 0.12


def published(**over):
    figs = {"temple": {"spot_volume": 34e6}, "cantex": {"spot_volume": 3.6e6, "tvl": 3.1e6},
            "pool-party": {"spot_volume": 2600.0, "tvl": 168_000.0}, "oneswap": {"tvl": 234_000.0},
            "rocky": {"perp_volume": 4.7e6, "spot_volume": 2.8e6}, "ekiden": {"perp_volume": 286_000.0}}
    for k, v in over.items():
        figs[k.replace("_", "-")].update(v)
    return {"venues": {s: {"t": int(NOW.timestamp()) - 600, "figures": f} for s, f in figs.items()}}


def fake_get(fail=(), **values):
    """Outside sources agreeing with ``published()`` unless ``values`` says otherwise."""
    v = {"temple": 34.2e6, "cantex_cc": 3.6e6 / CC, "pp_cc": 2600 / CC, "pp_res": 168_000 / CC / 2,
         "os_res": 234_000 / CC / 2, "rocky": 4.7e6, "ekiden": 286_000.0, "llama_cantex": 4.4e6,
         "llama_tvl": 2.8e6} | values
    calls = []

    def get(url, params=None):
        calls.append(url)
        for name in fail:
            if name in url:
                raise OSError("down")
        if url == cx.GECKO:
            return {"canton-network": {"usd": CC}}
        if url == cx.TEMPLE:
            return {"total_volume_usd": str(v["temple"]), "markets": []}
        if url == cx.CANTEX:
            return {"data": {"volume_cc": str(v["cantex_cc"])}}
        if url.endswith("/volume"):
            return {"perPool": {"Amulet-USDCx": {"volume": {"Amulet": str(v["pp_cc"]), "USDCx": "45"}},
                                "HECTO-USDC.B": {"volume": {"USDC.B": "101"}}}}
        if url.endswith("/tvl"):
            return {"pools": {"Amulet-USDCx": {"Amulet": str(v["pp_res"]), "USDCx": "1"},
                              "HECTO-USDC.B": {"HECTO": "9", "USDC.B": "9"}}}
        if url == cx.ONESWAP:
            cc = {"symbol": "CC", "id": "Amulet", "admin": "DSO::1220"}
            return [{"assetX": cc, "assetY": {"symbol": "HANDL", "id": "HANDL", "admin": "x"},
                     "reserveX": str(v["os_res"]), "reserveY": "1"},
                    {"assetX": {"symbol": "HECTO", "id": "HECTO", "admin": "h"}, "assetY": cc,
                     "reserveX": "1", "reserveY": "999999", "visible": False}]
        if url == cx.ROCKY_PERP:
            return [{"quoteVolume": str(v["rocky"] - 1e6)}, {"quoteVolume": "1000000"}]
        if url == cx.EKIDEN:
            return {"list": [{"turnover_24h": str(v["ekiden"])}]}
        if url == cx.LLAMA_DEXS:
            return {"protocols": [{"name": "Temple", "total24h": 34.5e6}, {"name": "Cantex", "total24h": v["llama_cantex"]},
                                  {"name": "Rocky Exchange Spot", "total24h": 2.6e6}, {"name": "Pool Party", "total24h": 2900}]}
        if url == cx.LLAMA_PROTOCOLS:
            return [{"name": "Cantex", "chainTvls": {"Canton": v["llama_tvl"]}}, {"name": "Pool Party", "chainTvls": {"Canton": 270_000}}]
        raise AssertionError(url)

    get.calls = calls
    return get


def test_silent_when_every_figure_agrees():
    text, rows = cx.run(published(), fake_get(), NOW)
    assert text is None
    checked = {(r["venue"], r["figure"]) for r in rows}
    assert {("temple", "spot_volume"), ("cantex", "spot_volume"), ("pool-party", "tvl"), ("oneswap", "tvl"),
            ("rocky", "perp_volume"), ("ekiden", "perp_volume"), ("cantex", "tvl")} <= checked
    # Pool Party's DefiLlama TVL counts non-CC pools too: never compared with our CC-pool liquidity
    assert not any(r["venue"] == "pool-party" and r["figure"] == "tvl" and "DefiLlama" in r["source"] for r in rows)
    # only CC legs and visible CC pools count, as on the card
    pp = next(r for r in rows if r["venue"] == "pool-party" and r["figure"] == "spot_volume" and "API" in r["source"])
    assert abs(pp["gap"]) < 0.01


def test_one_message_when_a_direct_figure_is_off_by_more_than_the_limit(monkeypatch):
    # Pool Party really holds 5 CC pools worth $169K; a card still showing $115K is 32% short
    text, _ = cx.run(published(pool_party={"tvl": 115_000.0}), fake_get(pp_res=169_000 / CC / 2), NOW)
    assert text.count("\n") == 1 and "pool-party tvl: card $115.0K" in text and "limit 15%" in text
    sent = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t0k3n")
    monkeypatch.setenv("CONTACT_CHAT_ID", "42")

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(url, data=None, timeout=None):
        sent.append((url, data))
        return Resp()

    monkeypatch.setattr(cx.urllib.request, "urlopen", urlopen)
    assert cx.send(text) and len(sent) == 1 and b"chat_id=42" in sent[0][1]


def test_defillama_gaps_use_their_own_looser_limit():
    # 20% under DefiLlama's Cantex volume: within LLAMA_GAP, silent
    assert cx.run(published(), fake_get(llama_cantex=4.5e6), NOW)[0] is None
    # 60% under: past LLAMA_GAP, reported
    text, _ = cx.run(published(), fake_get(llama_cantex=9e6), NOW)
    assert "cantex spot_volume" in text and "DefiLlama Canton DEX volume" in text and "limit 35%" in text


def test_a_failed_source_is_not_a_zero_and_stale_cards_are_reported():
    get = fake_get(fail=("ekiden", "templedigital"))
    text, rows = cx.run(published(), get, NOW)
    assert text is None and not any(r["venue"] in ("ekiden",) for r in rows)
    old = published()
    for v in old["venues"].values():
        v["t"] -= 3 * 3600
    text, _ = cx.run(old, fake_get(fail=("ekiden",)), NOW)
    assert "No card republished for 3.2 h" in text and "Not checked (source did not answer): ekiden" in text
    assert cx.run({}, fake_get(), NOW)[0].endswith("is empty).")


def test_no_secret_in_the_code_and_nothing_sent_without_one(monkeypatch):
    src = (Path(cx.__file__)).read_text()
    assert "api.telegram.org/bot{token}" in src and "os.getenv(\"TELEGRAM_BOT_TOKEN\")" in src
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("CONTACT_CHAT_ID", raising=False)
    assert cx.send("x") is False


def test_http_get_tries_again_after_a_server_error_but_not_after_a_client_error(monkeypatch):
    import io
    import urllib.error

    answers, waits = [], []

    def urlopen(req, timeout=None):
        code = answers.pop(0)
        if code != 200:
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b""))
        return io.BytesIO(b'{"ok": 1}')

    monkeypatch.setattr(cx.urllib.request, "urlopen", urlopen)
    answers[:] = [502, 200]  # Temple's first answer for a cold window, then the data
    assert cx.http_get("https://x", sleep=waits.append) == {"ok": 1} and waits == [2]
    answers[:] = [502, 502, 502]
    try:
        cx.http_get("https://x", sleep=waits.append)
        raise AssertionError("three server errors must raise")
    except urllib.error.HTTPError:
        pass
    answers[:] = [400]
    try:
        cx.http_get("https://x", sleep=waits.append)
        raise AssertionError("a client error is not tried again")
    except urllib.error.HTTPError:
        pass
    assert answers == []
