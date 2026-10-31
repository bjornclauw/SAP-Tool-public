"""Entry point. Keep this file tiny -- it should only ever wire the root
window together and start the mainloop."""

import os
import sys


def _ensure_std_streams() -> None:
    """pythonw.exe starts with sys.stdout/sys.stderr set to None. Give the
    app a silent null sink so nothing crashes; the toolbar's "Debug Console"
    checkbox replaces these with a real console when enabled."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")


if __name__ == "__main__":
    _ensure_std_streams()
    from ui.sap_toolbar import SapToolbar

    app = SapToolbar()
    app.mainloop()
