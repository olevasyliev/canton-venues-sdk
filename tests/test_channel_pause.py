"""F 2026-10-15: until our quotes are checked daily against the venues' own quote APIs, the channel
posts only what a venue's own API confirms. No execution or pegs post, no price-claim venue card, no
loud alert."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import channel  # noqa: E402


def test_no_price_post_while_paused():
    assert channel.PRICE_CLAIMS_PAUSED
    fri = datetime(2026, 10, 16, 9, 5, tzinfo=UTC)   # pegs
    wed = datetime(2026, 10, 21, 9, 5, tzinfo=UTC)   # execution
    mon = datetime(2026, 10, 19, 9, 5, tzinfo=UTC)   # weekly card: volumes
    assert channel.due({}, fri) is None and channel.due({}, wed) is None
    assert channel.due({}, mon) == "weekly"


def test_a_venue_card_with_a_price_headline_is_passed_over():
    heads = {"venues": {
        "temple": {"head": {"rule": "best_quote", "title": "Best price", "sub": "x"}},
        "cantex": {"head": {"rule": "kind_volume", "title": "The largest AMM", "sub": "$1.9M"}}}}
    post, turn = channel.venue_post(heads, 0, {"temple": 1, "cantex": 1})
    assert post is not None and "Cantex" in post.html and turn == 2


def test_no_loud_alert_goes_to_the_channel_while_paused():
    src = (Path(channel.__file__).parent / "collect.py").read_text()
    assert "[] if channel.PRICE_CLAIMS_PAUSED else al.channel(" in src
