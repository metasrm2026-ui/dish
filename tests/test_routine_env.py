"""scripts/routine-env.sh: the routines' .env from the cloud environment, the new names first, the
earlier DISH_ names while an environment still has them, and never a value on screen."""
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(tmp_path, **env):
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "routine-env.sh", tmp_path / "scripts")
    base = {k: v for k, v in os.environ.items() if not k.startswith(("METOMATE_", "DISH_")) and k != "APIFY_TOKEN"}
    p = subprocess.run(["bash", "scripts/routine-env.sh"], cwd=tmp_path, env={**base, **env}, capture_output=True, text=True)
    out = (tmp_path / ".env").read_text() if (tmp_path / ".env").exists() else None
    return p, out


def test_new_names_win_and_no_value_is_shown(tmp_path):
    p, out = run(tmp_path, METOMATE_DASHBOARD_URL="https://new.test", METOMATE_ROUTINE_SECRET="s3cret-new",
                 DISH_DASHBOARD_URL="https://old.test", DISH_ROUTINE_SECRET="s3cret-old", APIFY_TOKEN="ap-tok")
    assert p.returncode == 0
    assert out == "DASHBOARD_URL=https://new.test\nROUTINE_SECRET=s3cret-new\nAPIFY_TOKEN=ap-tok\n"
    assert "s3cret" not in p.stdout + p.stderr and "ap-tok" not in p.stdout + p.stderr
    assert oct((tmp_path / ".env").stat().st_mode & 0o777) == "0o600"


def test_the_earlier_names_still_work_one_by_one(tmp_path):
    p, out = run(tmp_path, METOMATE_DASHBOARD_URL="https://new.test", DISH_ROUTINE_SECRET="s3cret-old")
    assert p.returncode == 0 and out == "DASHBOARD_URL=https://new.test\nROUTINE_SECRET=s3cret-old\n"


def test_missing_variables_stop_and_name_them(tmp_path):
    p, out = run(tmp_path, METOMATE_DASHBOARD_URL="https://new.test")
    assert p.returncode == 1 and out is None
    assert "missing: METOMATE_ROUTINE_SECRET" in p.stderr
