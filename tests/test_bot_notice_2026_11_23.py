"""The line for bots in the API files they read (23.11): a new last field, nothing else changes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "canton_venues"))

import collect  # noqa: E402


def test_api_snapshot_files_get_the_notice_as_their_last_field(tmp_path):
    api = tmp_path / "api" / "v1"
    doc = {"t": 1, "pairs": {"CC/USDCx": [1, 2]}}
    for name in collect.NOTICE_FILES:
        collect.write_json(api / name, doc)
        got = json.loads((api / name).read_text())
        assert list(got) == ["t", "pairs", "notice"] and got["notice"] == collect.BOT_NOTICE
        assert {k: got[k] for k in doc} == doc
    assert doc == {"t": 1, "pairs": {"CC/USDCx": [1, 2]}}  # the caller's dict is not changed


def test_a_file_read_back_and_written_again_carries_one_notice(tmp_path):
    path = tmp_path / "api" / "v1" / "alerts.json"
    collect.write_json(path, {"active": [], "feed": []})
    collect.write_json(path, collect.load_json(path, {}))
    got = json.loads(path.read_text())
    assert list(got) == ["active", "feed", "notice"]


def test_running_records_state_files_and_lists_are_left_alone(tmp_path):
    api = tmp_path / "api" / "v1"
    for name in ("desk.json", "history.json", "venue_history.json", "weekly.json", "volume.json"):
        collect.write_json(api / name, {"t": 1})
        assert json.loads((api / name).read_text()) == {"t": 1}
    collect.write_json(tmp_path / "state" / "summary.json", {"t": 1})  # same name outside the API
    assert json.loads((tmp_path / "state" / "summary.json").read_text()) == {"t": 1}
    collect.write_json(api / "tokens.json", [1, 2])
    assert json.loads((api / "tokens.json").read_text()) == [1, 2]
