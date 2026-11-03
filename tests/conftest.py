import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bore.config import load_config  # noqa: E402

FIXTURES = Path(__file__).parent / "data"      # made-up data only
# Photos with real faces, for the few tests of the face model on real pixels. None is in the
# repository (no page's photos or documents may be): put licensed ones here to run those tests.
FACES = FIXTURES / "faces"


def face_photo(name: str) -> bytes:
    """tests/data/faces/<name>, or the test is skipped (it needs a real person's face)."""
    p = FACES / name
    if not p.exists():
        pytest.skip(f"needs a photo with a real face at tests/data/faces/{name} (none is in the repository)")
    return p.read_bytes()


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
