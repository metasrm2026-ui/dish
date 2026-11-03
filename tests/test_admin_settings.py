"""The screens show settings as the code uses them: a value typed into a page by hand must change when
the code's own setting does (an admin screen that said 8 posts a day while the routine made 20 is how
this was found). Posts a day are each page's budget now (docs/PRODUCT.md "Budgets"): the screens show
the page's own number from the server, and the approvals page's fallback is the dashboard's default."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "dashboard" / "public"


def test_posts_per_day_on_the_screens_is_the_pages_budget():
    default = re.search(r"BUDGET_DEFAULTS = \{ posts_per_day: (\d+),", (ROOT / "dashboard" / "lib" / "pages.js").read_text())
    assert default, "lib/pages.js no longer has BUDGET_DEFAULTS: update this test to where the default is"
    cap = re.search(r"^const CAP = (\d+);", (PUBLIC / "review.html").read_text(), re.M)
    assert cap, "review.html no longer has CAP: update this test to what the approvals page counts against"
    assert int(cap.group(1)) == int(default.group(1)), "change CAP in review.html to lib/pages.js BUDGET_DEFAULTS.posts_per_day"
    assert "posts_per_day || CAP" in (PUBLIC / "review.html").read_text(), "the page's own budget comes first"
    app = (PUBLIC / "app.js").read_text()
    assert not re.search(r"^const DAILY_POSTS = ", app, re.M), "the admin page shows the page's budget, not a number of its own"
    assert 'name="posts_per_day"' in app
