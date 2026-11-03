"""Only tested versions get installed: constraints.txt pins every package the routines use, every
install line uses it, and our own code uses no API its libraries have announced they will remove."""
import io
import re
import warnings
from pathlib import Path

import pytest
from packaging.markers import default_environment
from packaging.requirements import Requirement

from bore import deps
from bore.media import dhash

ROOT = Path(__file__).resolve().parent.parent


def reqs(name: str) -> list[Requirement]:
    lines = [ln.strip() for ln in (ROOT / name).read_text().splitlines()]
    return [Requirement(ln) for ln in lines if ln and not ln.startswith("#")]


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_every_requirement_is_pinned_for_the_routines_python():
    pins = {norm(r.name): r for r in reqs("constraints.txt")}
    env = {**default_environment(), "python_version": "3.11"}
    assert all(str(r.marker) == 'python_version == "3.11"' and len(r.specifier) == 1
               and next(iter(r.specifier)).operator == "==" for r in pins.values())
    for r in reqs("requirements.txt"):
        if r.marker and not r.marker.evaluate(env):
            continue
        pin = next(iter(pins[norm(r.name)].specifier)).version
        assert r.specifier.contains(pin), f"constraints.txt pins {r.name}=={pin}, outside {r}"


def test_every_install_uses_the_pins():
    script = (ROOT / "scripts" / "ensure-deps.sh").read_text()
    assert re.search(r"install\(\).*pip install[^\n]*-c constraints\.txt", script)
    block = (ROOT / "README.md").read_text().split("## Faster routine sessions")[1].split("```bash")[1].split("```")[0]
    installs = [ln for ln in block.splitlines() if "pip install" in ln]
    assert len(installs) == 2 and all("$pins" in ln for ln in installs) and "-c constraints.txt" in block
    # the setup line installs every package the routines check, under its pip name
    named = {norm(w.split("==")[0]) for w in installs[0].split() if re.match(r"[A-Za-z]", w)}
    assert {norm(name) for _, name in deps.CHECKS} <= named


def test_deprecations_in_our_code_fail_the_tests():
    with pytest.raises(DeprecationWarning):
        warnings.warn_explicit("going away", DeprecationWarning, "bore/x.py", 1, module="bore.x")


def test_photo_hashes_read_pixels_without_the_api_pillow_14_removes():
    import numpy as np
    from PIL import Image
    im = Image.fromarray(np.tile(np.arange(0, 256, 2, dtype=np.uint8), (96, 1))[:, ::-1].copy())  # bright to dark
    buf = io.BytesIO()
    im.save(buf, "PNG")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        h = dhash(buf.getvalue())
    px = np.asarray(Image.open(io.BytesIO(buf.getvalue())).convert("L").resize((9, 8)), dtype=int)
    assert h == sum(1 << i for i in range(64) if px[i // 8, i % 8] > px[i // 8, i % 8 + 1])
    assert h == 2 ** 64 - 1                                    # every pixel brighter than its right neighbour
