"""The menu's native header on macOS: logo · title · subtitle · an on/off switch.

pystray draws every row as plain text. AppKit lets ONE row carry a view
(`NSMenuItem.setView_`), and that is what Ollama, Docker and the like use for
their header. This module builds that view for the redesigned menu
(`menu_v2.HeaderNode`): the AbstractFramework logo (`logo.py`), the product
name in bold, a one-line subtitle (what the gateway is doing), and an
`NSSwitch` on the right that is ON while conversations run and OFF while the
gateway is paused — the gateway-wide pause (`POST /host/pause|resume`), the
only "off" that leaves the icon in place (the helper is the gateway's child;
a stopped gateway has no icon to turn it back on from).

Pure AppKit, called on the main thread by the icon's `_create_menu_item`
override (`app.py`); nothing here touches the network. Everything is
best-effort: when AppKit, `NSSwitch` (macOS 10.15+) or the logo cannot be
built, the caller falls back to the text header, so the menu always has one.
Windows and Linux never come here (pystray's Win32 and AppIndicator menus have
no per-item view).
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Tuple

from .menu_v2 import HeaderView

_TARGET_CLASS = None


def _switch_target_class():
    """The switch's Objective-C target class, registered ONCE per process
    (pyobjc refuses a second class with the same name; the menu is rebuilt
    many times)."""
    global _TARGET_CLASS
    if _TARGET_CLASS is None:
        import AppKit  # type: ignore[import-not-found]
        import Foundation  # type: ignore[import-not-found]
        import objc  # type: ignore[import-not-found]

        class AGTrayHeaderSwitchTarget(Foundation.NSObject):
            def initWithCallback_(self, cb):  # noqa: N802 - Objective-C selector
                self = objc.super(AGTrayHeaderSwitchTarget, self).init()
                if self is None:
                    return None
                self._cb = cb
                return self

            def toggled_(self, sender):  # noqa: N802 - Objective-C selector `toggled:`
                try:
                    self._cb(bool(sender.state() == AppKit.NSControlStateValueOn))
                except Exception:
                    pass

        _TARGET_CLASS = AGTrayHeaderSwitchTarget
    return _TARGET_CLASS


WIDTH = 300.0
HEIGHT = 58.0
LOGO_PT = 36.0
PAD_X = 14.0


def build_header_view(view: HeaderView, *, logo_png: bytes, on_toggle: Callable[[bool], None]) -> Optional[Tuple[Any, Tuple[Any, ...]]]:
    """(NSView, keep-alive objects) for the header row, or None when AppKit
    cannot build it. `on_toggle(True)` = the person switched it ON (resume),
    `on_toggle(False)` = OFF (pause)."""
    try:
        import AppKit  # type: ignore[import-not-found]
        import Foundation  # type: ignore[import-not-found]
        import objc  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        root = AppKit.NSView.alloc().initWithFrame_(((0.0, 0.0), (WIDTH, HEIGHT)))
        keep: list = []

        # Logo
        image = AppKit.NSImage.alloc().initWithData_(Foundation.NSData(logo_png))
        if image is not None:
            image.setSize_((LOGO_PT, LOGO_PT))
            iv = AppKit.NSImageView.alloc().initWithFrame_(((PAD_X, (HEIGHT - LOGO_PT) / 2.0), (LOGO_PT, LOGO_PT)))
            iv.setImage_(image)
            iv.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
            root.addSubview_(iv)
            keep += [image, iv]

        # Switch (right), built first so the text knows its room.
        switch_w = 0.0
        if view.switch_on is not None and hasattr(AppKit, "NSSwitch"):
            target = _switch_target_class().alloc().initWithCallback_(on_toggle)
            sw = AppKit.NSSwitch.alloc().initWithFrame_(((0.0, 0.0), (10.0, 10.0)))
            sw.sizeToFit()
            size = sw.frame().size
            sw.setFrameOrigin_((WIDTH - PAD_X - size.width, (HEIGHT - size.height) / 2.0))
            sw.setState_(AppKit.NSControlStateValueOn if view.switch_on else AppKit.NSControlStateValueOff)
            sw.setEnabled_(bool(view.switch_enabled))
            sw.setTarget_(target)
            sw.setAction_("toggled:")
            sw.setToolTip_(view.switch_tooltip)
            root.addSubview_(sw)
            keep += [target, sw]
            switch_w = size.width + 10.0

        # Title + subtitle
        text_x = PAD_X + LOGO_PT + 12.0
        text_w = WIDTH - text_x - PAD_X - switch_w
        title = AppKit.NSTextField.labelWithString_(view.title)
        title.setFont_(AppKit.NSFont.boldSystemFontOfSize_(13.0))
        title.setFrame_(((text_x, HEIGHT / 2.0 + 1.0), (text_w, 18.0)))
        title.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        root.addSubview_(title)
        sub = AppKit.NSTextField.labelWithString_(view.subtitle)
        sub.setFont_(AppKit.NSFont.systemFontOfSize_(11.0))
        sub.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        sub.setFrame_(((text_x, HEIGHT / 2.0 - 17.0), (text_w, 16.0)))
        sub.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        sub.setToolTip_(view.subtitle)
        root.addSubview_(sub)
        keep += [title, sub]
        return root, tuple(keep)
    except Exception:
        return None
