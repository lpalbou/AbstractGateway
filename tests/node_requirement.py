"""The console / JavaScript behaviour tests need `node`.

A missing `node` used to SKIP those tests silently, so a local run without
Node went green while checking none of the console's behaviour. Now a missing
`node` FAILS them with the fix, unless the run opts out explicitly with
`--allow-skip-js` (tests/conftest.py), in which case they skip with the
reason. CI has node.
"""

from __future__ import annotations

import shutil

import pytest

MESSAGE = "node is required for the console behaviour tests; install Node >= 20 or pass --allow-skip-js explicitly"

# Set by tests/conftest.py from the `--allow-skip-js` option.
ALLOW_SKIP_JS = False


def require_node() -> str:
    """The `node` executable, or a failure (a skip under --allow-skip-js)."""
    node = shutil.which("node")
    if node:
        return node
    if ALLOW_SKIP_JS:
        pytest.skip(f"{MESSAGE} (skipped: --allow-skip-js)")
    pytest.fail(MESSAGE, pytrace=False)
