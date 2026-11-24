"""Entry point. Keep this file tiny -- it should only ever wire the root
window together and start the mainloop."""

import os
import sys


def _finish_dependencies() -> None:
    """Complete a locked wheel install before any feature modules load."""
    from core import i18n, updater

    error = updater.finish_pending_dependencies()
    if not error:
        return
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        i18n.t("dependencies.title", "Runtime dependencies"),
        i18n.t(
            "dependencies.restart_failed",
            "Dependencies could not be installed: {error}\n\n"
            "Restart the app and try again.",
            error=error,
        ),
        parent=root,
    )
    root.destroy()
    raise SystemExit(1)


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
    _finish_dependencies()
    from ui.sap_toolbar import SapToolbar

    app = SapToolbar()
    app.mainloop()
