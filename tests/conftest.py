import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bore.config import load_config  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cfg():
    return copy.deepcopy(load_config())


@pytest.fixture
def store(tmp_path):
    from bore.store import Store
    return Store(tmp_path / "t.db")


@pytest.fixture(autouse=True)
def _no_reference_faces(monkeypatch):
    """The subject's reference face comes from the network (ESPN, Wikipedia): never in tests."""
    from bore import faceid
    monkeypatch.setattr(faceid, "reference", lambda name, sports=False, player=None, timeout=6: (None, ""))


# Tests of the pages' own settings (sources, playbooks, learned rules) need the private config
# that `bore studio sync` pulls from the dashboard. Without it (a developer machine) they skip.
from bore.config import PRIVATE  # noqa: E402

BRAND_TESTS = (
    "tests/test_krea_art.py::test_art_prompt_uses_page_style_team_colours_and_no_people",
    "tests/test_prefilter.py::test_culture_rush_junk_is_dropped_and_approved_kinds_rise",
    "tests/test_prefilter.py::test_huddle_logistics_and_podcasts_are_dropped",
    "tests/test_repeats.py::",
    "tests/test_review_lessons.py::test_playbooks_carry_the_learned_kinds",
    "tests/test_review_lessons.py::test_trade_only_industry_news_is_dropped_unless_big",
    "tests/test_routine_cards.py::test_krea_prompt_has_exact_text_and_people_rules",
    "tests/test_source_tiers.py::test_culture_rush_sheet_is_wired_within_budget",
)


def pytest_collection_modifyitems(config, items):
    if (PRIVATE / "pages.yaml").exists():
        return
    skip = pytest.mark.skip(reason="needs the pages' private config (run `python -m bore.cli studio sync`)")
    for it in items:
        if it.nodeid.startswith(BRAND_TESTS):
            it.add_marker(skip)
