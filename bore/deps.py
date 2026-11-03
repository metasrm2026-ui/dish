"""The routines' Python packages: which ones are missing or do not load on this machine.

A package can be installed and still not load. The cloud machines carry a system Pillow built for
another Python (in /usr/lib/python3/dist-packages, which every Python reads): `import PIL` works,
`from PIL import Image` fails, and pip counts it as installed, so a plain install skips it. Each
package is therefore checked by importing the part that needs its compiled code. Loading is not
working either: the machine tests (SELFTESTS) open and save an image, read a tiny card with OCR and
start the browser every card is drawn in (Chromium, which is not a pip package at all).

    python -m bore.deps            the requirements.txt lines to install, one per line (none: all fine);
                                   a package that fails only because it imports a broken one is left out
    python -m bore.deps --why      what fails and where each package was found (and versions that
                                   are not the tested ones in constraints.txt)
    --selftest                     also the machine tests (a failing test names the package to install
                                   again; "chromium" names none: `python -m playwright install` repairs it)
    --only requests,PyYAML         just these packages or tests (the publisher needs no image packages)
    --requirements                 the requirements.txt lines of the packages checked, broken or not
    --drift                        the installed versions that are not the tested ones, one per line

Standard library only: it runs before anything is installed (the tests import what they test).
"""
from __future__ import annotations

import contextlib
import importlib
import importlib.metadata
import importlib.util
import os
import re
import sys
import warnings
from pathlib import Path

REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"
CONSTRAINTS = REQUIREMENTS.with_name("constraints.txt")

# (what to import, its name in requirements.txt); the import reaches the compiled code
CHECKS = [
    ("requests", "requests"),
    ("yaml", "PyYAML"),
    ("openpyxl", "openpyxl"),
    ("reportlab.pdfgen.canvas", "reportlab"),
    ("playwright.sync_api", "playwright"),
    ("PIL.Image", "Pillow"),
    ("numpy", "numpy"),
    ("cv2", "opencv-python"),
    ("rapidocr_onnxruntime", "rapidocr-onnxruntime"),
    ("pypdfium2", "pypdfium2"),           # studio sync draws the reference documents' pages
]
# what the machine must do with them (functions below): (test, name, the packages it uses, the package
# to install again when it fails). Tried only when asked for by name (`--selftest`, NEEDS).
SELFTESTS = [
    ("selftest_image", "image files", ("Pillow",), "Pillow"),
    ("selftest_ocr", "ocr", ("Pillow", "rapidocr-onnxruntime"), "rapidocr-onnxruntime"),
    ("selftest_render", "chromium", ("playwright", "PyYAML"), None),
]
# what each Studio job needs before it is claimed: COMPOSE draws (playwright driving Chromium) and reads
# its card back (OCR), IMAGES checks photos (faces, OCR), CHECK downloads the card, IDEAS needs none,
# TEMPLATES draws each template's preview (Chromium) next to the document pages `studio sync` drew.
# The OCR test costs about a second once per process, and loads the reader these steps use anyway.
NEEDS = {"compose": ("Pillow", "image files", "numpy", "opencv-python", "rapidocr-onnxruntime", "ocr", "playwright",
                     "chromium"),
         "images": ("Pillow", "image files", "numpy", "opencv-python", "rapidocr-onnxruntime", "ocr"),
         "check": ("Pillow", "image files"), "ideas": (),
         "templates": ("Pillow", "image files", "pypdfium2", "playwright", "chromium")}


@contextlib.contextmanager
def _quiet_stdout():
    """Imports that fail can print (cv2 prints install advice, even sys.path), and stdout is the
    list of requirement lines: drop what they print, from Python or from C (--why says why)."""
    sys.stdout.flush()
    try:
        saved, null = os.dup(1), os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, 1)
        os.close(null)
    except OSError:
        saved = None
    try:
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
            yield
    finally:
        if saved is not None:
            os.dup2(saved, 1)
            os.close(saved)


def _why(e: Exception) -> str:
    return " ".join(f"{type(e).__name__}: {e}".split())[:300]


def problems(names=None) -> dict[str, str]:
    """{name: why it does not work} for the packages that are missing or broken, and the machine
    tests named in `names` that fail (None: every package, no tests). A test is not tried while a
    package it uses does not load: that package is the problem."""
    out = {}
    with warnings.catch_warnings(), _quiet_stdout():   # a mismatched build warns before it fails
        warnings.simplefilter("ignore")
        for module, name in CHECKS:
            if names is not None and name not in names:
                continue
            try:
                importlib.import_module(module)
            except Exception as e:      # ImportError, or a compiled part that fails as it loads
                out[name] = _why(e)
        for fn, name, uses, _ in SELFTESTS:
            if names is not None and name in names and not any(u in out for u in uses) and _tried(fn):
                out[name] = _tried(fn)
    return out


def require(names, what: str) -> None:
    """Stop `what` (exit non-zero) when these packages or tests fail here: carrying on would turn a
    broken machine into unchecked or missing photos, reported as success."""
    bad = problems(names)
    if bad:
        raise SystemExit(f"{what}: not working on this machine ({'; '.join(f'{k}: {v}' for k, v in bad.items())}). "
                         "Run `bash scripts/ensure-deps.sh`, then this step again; if it still fails, report it and stop.")


_TRIED: dict[str, str] = {}


def _tried(fn: str) -> str:
    """'' when the machine test passes, else why not. Once per process: a browser that does not
    start can take its whole launch timeout, and Studio checks every job it looks at."""
    if fn not in _TRIED:
        try:
            globals()[fn]()
            _TRIED[fn] = ""
        except Exception as e:
            _TRIED[fn] = _why(e)
    return _TRIED[fn]


def _test_card():
    """A small white image saying 42 in black."""
    from PIL import Image, ImageDraw, ImageFont
    try:
        font, scale = ImageFont.load_default(size=40), 1
    except (TypeError, OSError, ImportError):   # old or FreeType-less Pillow: a bitmap font, drawn and enlarged
        font, scale = ImageFont.load_default(), 4
    im = Image.new("RGB", (120 // scale, 60 // scale), "white")
    ImageDraw.Draw(im).text((10 // scale, 6 // scale), "42", fill="black", font=font)
    return im.resize((120, 60)) if scale > 1 else im


def selftest_image():
    """test: save a JPEG and a PNG with Pillow and open them again"""
    import io

    from PIL import Image
    for fmt in ("JPEG", "PNG"):
        buf = io.BytesIO()
        _test_card().save(buf, fmt)
        im = Image.open(io.BytesIO(buf.getvalue()))
        im.load()
        if im.size != (120, 60):
            raise RuntimeError(f"a {fmt} saved at 120x60 opened at {im.size}")


def selftest_ocr():
    """test: read a tiny card's text with OCR (what `routine verify` does)"""
    import tempfile

    from .guard import _ocr
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        _test_card().save(f.name)
        if not any(text.strip() for text, _, _ in _ocr(f.name)):
            raise RuntimeError("OCR read nothing on a test card saying 42")


def selftest_render():
    """test: start headless Chromium and take a screenshot (every card is drawn in it)"""
    from playwright.sync_api import sync_playwright

    from .cards import _chrome_path
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**({"executable_path": _chrome_path()} if _chrome_path() else {}), timeout=30000)
        try:
            pg = browser.new_page(viewport={"width": 8, "height": 8})
            pg.set_content("<body style='margin:0;background:#c00'></body>")
            png = pg.screenshot(type="png")
        finally:
            browser.close()
    if not png.startswith(b"\x89PNG"):
        raise RuntimeError("Chromium started but took no screenshot")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requires(name: str) -> set[str]:
    """The packages this one's installed metadata says it needs (no extras)."""
    try:
        reqs = importlib.metadata.requires(name) or []
    except Exception:
        return set()
    return {_norm(m.group(0)) for r in reqs if not re.search(r"extra\s*==", r)
            for m in [re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", r)] if m}


def causes(bad: dict[str, str]) -> dict[str, str]:
    """The packages broken in themselves: not those that fail only because they need another broken
    one (reportlab and rapidocr fail while Pillow does, opencv while numpy does; they load once it
    is fixed). Known from the package's metadata, else from its error naming the other package."""
    tops = {name: module.split(".")[0] for module, name in CHECKS}

    def consequence(name: str, why: str) -> bool:
        needs = _requires(name)
        return any(other != name and (_norm(other) in needs or re.search(rf"\b{re.escape(tops.get(other, other))}\b", why))
                   for other in bad)
    roots = {name: why for name, why in bad.items() if not consequence(name, why)}
    return roots or bad


def drift(names) -> list[str]:
    """'name installed (constraints.txt: pinned)' for these packages and the ones they need, where the
    installed version is not the tested one: an install without the pins (a setup script run outside
    the repository folder) works until a new version does not. Only for the Python the pins are for."""
    here = f"{sys.version_info.major}.{sys.version_info.minor}"
    try:
        lines = CONSTRAINTS.read_text().splitlines()
    except OSError:
        return []
    pins = {}
    for line in lines:
        m = re.match(r'([A-Za-z0-9_.-]+)==([^;\s]+)\s*(?:;\s*python_version\s*==\s*"([\d.]+)")?\s*$', line.strip())
        if m and m.group(3) in (None, here):
            pins[_norm(m.group(1))] = m.group(2)
    out, seen, todo = [], set(), [_norm(n) for n in names]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            have = importlib.metadata.version(name)
        except Exception:             # not installed: the checks say so
            continue
        if name in pins and have != pins[name]:
            out.append(f"{name} {have} (constraints.txt: {pins[name]})")
        todo += _requires(name)
    return sorted(out)


def requirement_lines(names) -> list[str]:
    """The requirements.txt lines (pins and markers kept) of these packages."""
    want = {n.lower() for n in names}
    lines = []
    for raw in REQUIREMENTS.read_text().splitlines():
        line = raw.strip()
        m = re.match(r"([A-Za-z0-9_.-]+)", line)
        if line and not line.startswith("#") and m and m.group(1).lower() in want:
            lines.append(line)
    return lines


def _where(module: str) -> str:
    top = module.split(".")[0]
    try:
        spec = importlib.util.find_spec(top)
    except Exception:
        spec = None
    return spec.origin if spec and spec.origin else "not found"


def main(argv: list[str]) -> int:
    only = argv[argv.index("--only") + 1].split(",") if "--only" in argv else None
    tests = [name for _, name, _, _ in SELFTESTS if "--selftest" in argv or only is not None]
    names = [name for name in [n for _, n in CHECKS] + tests if only is None or name in only]
    if "--requirements" in argv:
        print("\n".join(requirement_lines(names)))
        return 0
    if "--drift" in argv:
        for line in drift([n for _, n in CHECKS if n in names]):
            print(line)
        return 0
    bad = problems(names)
    if "--why" in argv:
        print(f"python {sys.version.split()[0]} at {sys.executable}")
        for module, name in CHECKS:
            if name not in names:
                continue
            print(f"{'FAIL' if name in bad else 'ok  '} {name:22} {_where(module)}" + (f"  ({bad[name]})" if name in bad else ""))
        for fn, name, _, _ in SELFTESTS:
            if name in names:       # "--": not tried, a package it uses does not load
                state = "FAIL" if name in bad else "ok  " if fn in _TRIED else "--  "
                print(f"{state} {name:22} {globals()[fn].__doc__}" + (f"  ({bad[name]})" if name in bad else ""))
        for line in drift([n for _, n in CHECKS if n in names]):
            print(f"note {line}: not the tested version")
        return 1 if bad else 0
    # a failing test is repaired by installing its package again ("chromium" has no requirement line)
    repair = {name: pkg for _, name, _, pkg in SELFTESTS}
    for line in requirement_lines({repair.get(n, n) for n in causes(bad)} - {None}):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
