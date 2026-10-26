"""`serve --gc-tuning on|off` (backlog 1054): freeze what the boot built and raise the collection thresholds,
or leave the interpreter's garbage collector exactly as it is."""

from __future__ import annotations

import gc

import pytest

from abstractgateway import service


@pytest.fixture(autouse=True)
def _restore_gc():
    thresholds = gc.get_threshold()
    choice = service._gc_tuning_on
    gc.unfreeze()
    yield
    gc.unfreeze()
    gc.set_threshold(*thresholds)
    service._gc_tuning_on = choice


def test_on_freezes_and_sets_the_thresholds_it_reports(capsys):
    assert gc.get_freeze_count() == 0
    gc.set_threshold(700, 10, 10)
    before = gc.get_threshold()
    out = service.apply_gc_tuning(on=True)

    assert out["applied"] is True
    assert gc.get_threshold() == service.GC_TUNED_THRESHOLDS
    assert out["thresholds"] == service.GC_TUNED_THRESHOLDS
    assert out["thresholds_before"] == before
    assert gc.get_freeze_count() > 0
    assert out["frozen"] > 0  # the permanent generation shrinks as frozen objects die: no equality
    err = capsys.readouterr().err
    assert "[INFO] gc: froze" in err and "/".join(map(str, service.GC_TUNED_THRESHOLDS)) in err
    assert service.gc_tuning_report()["applied"] is True


def test_off_leaves_gc_untouched(capsys):
    gc.set_threshold(700, 10, 10)
    out = service.apply_gc_tuning(on=False)

    assert out["applied"] is False
    assert gc.get_threshold() == (700, 10, 10)
    assert gc.get_freeze_count() == 0
    assert "tuning off" in capsys.readouterr().err


def test_the_serve_choice_decides_when_no_argument_is_given():
    gc.set_threshold(700, 10, 10)
    service.set_gc_tuning(False, source="test")
    assert service.apply_gc_tuning()["applied"] is False
    assert gc.get_threshold() == (700, 10, 10) and gc.get_freeze_count() == 0

    service.set_gc_tuning(True, source="test")
    assert service.apply_gc_tuning()["applied"] is True
    assert gc.get_threshold() == service.GC_TUNED_THRESHOLDS


def test_a_boot_not_started_by_serve_never_tunes(monkeypatch):
    calls = []
    monkeypatch.setattr(service, "apply_gc_tuning", lambda on=None: calls.append(on) or {})
    monkeypatch.setattr(service, "_gc_tuning_on", None)
    service._apply_gc_tuning_after_boot()
    assert not [t for t in __import__("threading").enumerate() if t.name == "gateway-gc-tuning"]
    assert calls == []


@pytest.mark.parametrize("choice", [True, False])
def test_a_serve_boot_applies_its_choice_once(monkeypatch, choice):
    calls = []
    monkeypatch.setattr(service, "apply_gc_tuning", lambda on=None: calls.append(on) or {})
    monkeypatch.setattr(service, "_rehydrate_thread", None)
    monkeypatch.setattr(service, "_gc_tuning_on", choice)
    service._apply_gc_tuning_after_boot()
    for t in __import__("threading").enumerate():
        if t.name == "gateway-gc-tuning":
            t.join(timeout=5)
    assert calls == [None]


@pytest.mark.parametrize("flag,expected", [(["--gc-tuning", "off"], False), (["--gc-tuning", "on"], True), ([], True)])
def test_serve_flag_reaches_the_service(monkeypatch, flag, expected):
    from abstractgateway import cli
    from abstractgateway.push import settings as push_settings

    seen = []

    class _Stop(Exception):
        pass

    def _stop(*_a, **_k):
        raise _Stop()

    monkeypatch.setattr(cli, "_reserve_gguf_metal", lambda: None)
    monkeypatch.setattr(cli, "_migrate_legacy_core_config_store", lambda: None)
    monkeypatch.setattr(service, "set_gc_tuning", lambda on, source="serve": seen.append((on, source)))
    monkeypatch.setattr(push_settings, "set_push_relay", _stop)
    with pytest.raises(_Stop):
        cli.main(["serve", *flag])
    assert seen == [(expected, "serve")]
