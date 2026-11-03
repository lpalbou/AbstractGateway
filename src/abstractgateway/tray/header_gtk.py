"""The menu header on Linux: the logo beside the product name.

pystray's GTK and AppIndicator backends build `Gtk.MenuItem`s. An
AppIndicator menu is not drawn by this process: it is exported over DBus
(dbusmenu) and the desktop panel draws it, so a row can carry a label, an
icon and a check mark — nothing more (no switch, no second line). The
header is therefore the logo (`logo.py`) as the row's image and the name as
its label; the subtitle is the next row and the switch is the `☑ Running`
check row (`menu_v2.toggle_row`).

Best-effort: when GdkPixbuf or Gtk.ImageMenuItem is not there, the caller
keeps pystray's plain row. Not exercised on this operator's macOS; the
AppIndicator export of `GtkImageMenuItem` images (`icon-data`) is
libdbusmenu-gtk's documented behaviour.
"""

from __future__ import annotations

from typing import Any, Optional

LOGO_PX = 24


def header_menu_item(label: str, *, logo_png: bytes) -> Optional[Any]:
    """A `Gtk.ImageMenuItem` with the logo and `label` in bold, insensitive
    (an information row), or None when it cannot be built."""
    try:
        import gi  # type: ignore[import-not-found]

        gi.require_version("Gtk", "3.0")
        from gi.repository import GdkPixbuf, GLib, Gtk  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(logo_png)
        loader.close()
        pixbuf = loader.get_pixbuf().scale_simple(LOGO_PX, LOGO_PX, GdkPixbuf.InterpType.BILINEAR)
        item = Gtk.ImageMenuItem.new_with_label(label)
        item.set_image(Gtk.Image.new_from_pixbuf(pixbuf))
        item.set_always_show_image(True)
        children = item.get_children()
        if children:
            children[0].set_markup("<b>%s</b>" % GLib.markup_escape_text(label))
        item.set_sensitive(False)
        return item
    except Exception:
        return None
