"""A product with no page yet (the dashboard synced, none connected) is not an error: the hourly
commands find nothing to do. Before any sync, the commands still stop and say to sync."""
import types

import pytest

from bore import cli, config


def test_synced_with_no_pages_is_nothing_to_do(tmp_path, monkeypatch):
    f = tmp_path / "pages.yaml"
    f.write_text("pages: []\n")
    monkeypatch.setattr(config, "pages_path", lambda: f)
    monkeypatch.setattr(cli, "load_pages", lambda: config.load_pages(f))
    args = types.SimpleNamespace(page=None)
    assert cli._pages(args, {}, running=True) == [] and cli._pages(args, {}) == []
    with pytest.raises(SystemExit, match="unknown page x"):
        cli._pages(types.SimpleNamespace(page="x"), {})


def test_never_synced_still_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "pages_path", lambda: tmp_path / "missing.yaml")
    monkeypatch.setattr(cli, "load_pages", lambda: [])
    with pytest.raises(SystemExit, match="studio sync"):
        cli._pages(types.SimpleNamespace(page=None), {}, running=True)
