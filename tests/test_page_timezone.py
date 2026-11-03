"""A page's time zone the routines' machines do not know ("Eastern" typed by hand in the admin
page, an old alias name) never stops the hourly posts: every page's picks are worked out in one
loop, and one ZoneInfo error there stopped them for every page."""
from bore import ops
from bore.cli import page_quota
from bore.config import Page
from bore.studio import page_from_config


def test_an_unknown_zone_is_the_default_with_a_degraded_warning(cfg, store, capsys):
    ops._DEGRADED.clear()
    page = page_from_config({"slug": "p", "name": "P", "timezone": "Eastern", "pipeline": {"hourly": True}})
    assert page.timezone == "America/New_York"
    assert page_quota(page, store, cfg, 1_790_000_000.0) >= 0          # no ZoneInfoNotFoundError
    assert ops._DEGRADED == ["page p: unknown time zone 'Eastern', used America/New_York"]
    assert "set it under Admin > Pages" in capsys.readouterr().err
    ops._DEGRADED.clear()


def test_old_names_are_read_as_their_new_ones_and_good_ones_kept():
    ops._DEGRADED.clear()
    assert Page.from_dict({"slug": "p", "name": "P", "timezone": "US/Eastern"}).timezone == "America/New_York"
    assert Page.from_dict({"slug": "p", "name": "P", "timezone": "Asia/Calcutta"}).timezone == "Asia/Kolkata"
    assert Page.from_dict({"slug": "p", "name": "P", "timezone": "Europe/London"}).timezone == "Europe/London"
    assert Page.from_dict({"slug": "p", "name": "P"}).timezone == "America/New_York"
    assert ops._DEGRADED == []
