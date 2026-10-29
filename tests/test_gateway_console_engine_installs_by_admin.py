"""Operator ruling 2026-10-29: engines are installed ONCE per computer by the gateway's administrator
and shared by every user, so the Engines tab has no "Allow engine installs" switch (root 0858 is
withdrawn). An account that is not an admin reads one sentence; an admin sees the install actions only.
Driven on the SHIPPED function (sliced from the served console) under node.
"""

from __future__ import annotations

import pytest

from test_gateway_console_offline import _console_script, _node, _slice_function

pytestmark = pytest.mark.basic


def _markup(admin: bool) -> str:
    source = _console_script()
    script = f"""
const state = {{ principal: {{ admin: {str(admin).lower()} }} }};
{_slice_function(source, "engineInstallSettingMarkup")}
console.log(JSON.stringify([engineInstallSettingMarkup()]));
"""
    return _node(script)[0]


def test_a_non_admin_reads_who_installs_engines() -> None:
    html = _markup(admin=False)
    assert "An administrator installs engines for this computer." in html
    assert 'role="switch"' not in html and "allow-install" not in html


def test_an_admin_sees_no_switch() -> None:
    assert _markup(admin=True) == ""


def test_the_console_has_no_engine_install_switch_left() -> None:
    source = _console_script()
    for gone in ("allow-install", "engineInstallSettingSave", "engineInstallSettingRefresh", "Allow engine installs"):
        assert gone not in source, gone
