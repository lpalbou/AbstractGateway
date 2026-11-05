"""The FastAPI app reports the package's own version (the 0.20.0 tag was refused by the release workflow:
`app.py` still said 0.19.0). Red the moment the three version strings drift: pyproject, `__version__` and
the app's `version=` (the literal release.yml reads)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic

ROOT = Path(__file__).resolve().parents[1]


def test_the_app_version_is_the_package_version() -> None:
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib

    package = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    from abstractgateway import __version__
    from abstractgateway.app import app

    assert __version__ == package
    assert app.version == package
    # The literal the release workflow validates (`version="..."` in app.py), not only the attribute.
    literal = re.search(r'version\s*=\s*"([^"]+)"', (ROOT / "src/abstractgateway/app.py").read_text(encoding="utf-8"))
    assert literal is not None and literal.group(1) == package
