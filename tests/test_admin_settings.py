"""The admin page shows settings as the code uses them: a value typed into the page by hand must
change when the routine's own setting does (an admin screen that said 8 posts a day while the
routine made 20 is how this was found)."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_posts_per_day_on_the_admin_page_is_the_routines_cap():
    cap = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())["posting"]["daily_cap"]
    shown = re.search(r"^const DAILY_POSTS = (\d+);", (ROOT / "dashboard" / "public" / "app.js").read_text(), re.M)
    assert shown, "app.js no longer has DAILY_POSTS: update this test to what the admin page shows"
    assert int(shown.group(1)) == cap, "change DAILY_POSTS in dashboard/public/app.js to config.yaml posting.daily_cap"
