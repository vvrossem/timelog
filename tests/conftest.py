"""Fixtures: a fake aw-server, and a day directory and config of its own per test"""
import os

import pytest

os.environ["TIMELOG_CONFIG"] = os.devnull  # the defaults, not the user's config: before timelog is imported

from timelog import config, sources  # noqa: E402
from timelog.sources import activitywatch  # noqa: E402


@pytest.fixture
def days(tmp_path, monkeypatch):
    path = tmp_path / "days"
    monkeypatch.setattr(config, "DAY_DIR", str(path))
    monkeypatch.setattr(config, "BACKUPS", str(path / "backups"))
    monkeypatch.setattr(config, "CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(config, "RULES", [])
    monkeypatch.setattr(sources, "reviews", lambda: [])  # GitHub isn't asked
    return path


@pytest.fixture
def aw(monkeypatch):
    """a fake aw-server: set aw[<bucket type>] to its events"""
    store = {}
    buckets = {"agents_h": {"type": "agent.turn", "hostname": "h"}, "afk_h": {"type": "afkstatus", "hostname": "h"},
               "web_h": {"type": "web.tab.current", "hostname": "h"}, "win_h": {"type": "currentwindow", "hostname": "h"},
               "edit_h": {"type": "app.editor.activity", "hostname": "h"},
               "other_x": {"type": "agent.turn", "hostname": "x"}}

    def fetch(path, params=None):
        if path == "/info":
            return {"hostname": "h"}
        if path == "/buckets/":
            return buckets
        bucket = path.split("/")[2]
        return [dict(e) for e in store.get(buckets[bucket]["type"], []) if bucket != "other_x"]

    monkeypatch.setattr(activitywatch, "fetch", fetch)
    return store

