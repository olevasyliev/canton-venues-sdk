"""Best execution's "Trade here" link: every venue gets the same plain link to its own site.

The link must point where the venue's own page points (venue_pages.VENUES "site"), carry no
referral or tracking parameter, and its clicks are counted as their own event.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "examples" / "canton_venues"
sys.path.insert(0, str(ROOT))

import venue_pages as vp  # noqa: E402

PAGE = (ROOT / "site" / "index.html").read_text()


def _vurl() -> dict[str, str]:
    m = re.search(r"const VURL = \{(.*?)\};", PAGE)
    assert m, "VURL map missing from index.html"
    return dict(re.findall(r'(\w+): "([^"]+)"', m.group(1)))


def test_every_venue_links_to_its_own_site() -> None:
    vurl = _vurl()
    for venue in vp.VENUES:
        for row in venue["rows"]:
            if row.endswith("_perp"):
                continue  # perps have no best-execution row
            assert vurl.get(row) == venue["site"], (row, vurl.get(row), venue["site"])


def test_links_carry_no_referral_or_tracking() -> None:
    for url in _vurl().values():
        assert "?" not in url and "#" not in url, url


def test_trade_here_column_and_event() -> None:
    assert "<th>Trade here</th>" in PAGE
    assert 'class="tradehere"' in PAGE
    assert 'return "trade-here"' in PAGE
    # the event says which trade (pair, side, size); not data-pair/-side/-size, which the page's
    # own click handler reads to switch the table
    assert "data-trade=" in PAGE and "a.dataset.trade" in PAGE
    # the same link for every venue, said on the page
    assert "we route nothing and take no fee" in PAGE
