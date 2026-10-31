"""Backend abstraction for talking to SAP GUI Scripting.

One interpreter, one adapter. ``RealSapBackend`` wraps the SAP GUI
Scripting COM API via win32com and only imports pywin32 lazily, so this
module still loads fine on a machine without SAP GUI installed.

The test/fake seam lives *below* the adapter, not beside it: pass
``app_factory`` a zero-arg callable returning the scripting engine and the
adapter talks to that object graph instead of real COM (see
``core/fake_com.py``). That keeps ``_find``/``set_property``/``call_method``
/``get_property``/``get_session_property`` on one code path whether the
engine talks to SAP or the fake.

The engine never imports win32com directly -- it only talks to whichever
backend is active through this interface.
"""

from __future__ import annotations

import gc
import logging
from typing import Any

logger = logging.getLogger("sap_backend")


class SapBackend:
    """Interface every backend must implement."""

    def connect(self) -> bool:
        raise NotImplementedError

    def is_connected(self) -> bool:
        raise NotImplementedError

    def set_property(self, path: str, member: str, value: Any) -> None:
        raise NotImplementedError

    def call_method(self, path: str, member: str, arg: Any | None) -> Any:
        raise NotImplementedError

    def get_property(self, path: str, member: str) -> Any:
        raise NotImplementedError

    def get_session_property(self, member: str) -> Any:
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError


class RealSapBackend(SapBackend):
    """Talks to a running SAP GUI via win32com -- or to a fake COM graph.

    Usable on Windows with SAP GUI open and scripting enabled (Options >
    Accessibility & Scripting > Scripting). Pass ``app_factory`` to inject a
    stand-in scripting engine (the fake); with the default the adapter
    attaches to the running SAP GUI via ``GetObject("SAPGUI")``.

    Attach is per-run and holds nothing between runs: the running SAP GUI
    keeps its own windows, so re-locating it each time is cheap and there is
    no lingering scripting attachment (which is what makes SAP show
    "scripting active" and swap the native file dialogs for its own).
    """

    def __init__(self, app_factory=None):
        self._app_factory = app_factory
        self._connected = False
        self._session = None
        self._connection = None
        self._sap_gui_auto = None
        self._application = None

    def connect(self) -> bool:
        if self._app_factory is not None:
            return self._connect_fake()
        return self._connect_real()

    def _connect_fake(self) -> bool:
        try:
            if self._application is None:
                self._application = self._app_factory()
            self._connection = self._application.Children(0)
            self._session = self._connection.Children(0)
            self._connected = True
            logger.info("Fake SAP connected")
            return True
        except Exception as exc:  # noqa: BLE001 -- the fake must never crash the app
            logger.warning("Fake SAP connection failed: %s", exc)
            self._connected = False
            self._session = None
            self._connection = None
            self._application = None
            return False

    def _connect_real(self) -> bool:
        try:
            import win32com.client  # noqa: F401 (local import -- optional dependency)
        except ImportError as exc:
            raise RuntimeError(
                "pywin32 is required for real SAP mode. Install it with "
                "'pip install pywin32', or switch Fake SAP back on."
            ) from exc

        try:
            # Attach fresh every time: SAP GUI holds its own windows, so
            # re-locating the running instance is cheap and holding a scripting
            # handle between runs is exactly what made SAP show scripting-active.
            self._sap_gui_auto = win32com.client.GetObject("SAPGUI")
            self._application = self._sap_gui_auto.GetScriptingEngine
            self._connection = self._application.Children(0)
            self._session = self._connection.Children(0)
            self._connected = True
            logger.info("SAP attached via COM")
            return True
        except Exception as exc:  # pragma: no cover -- depends on a live SAP GUI session
            logger.warning("SAP attach failed: %s", exc)
            self._connected = False
            # Drop everything so the next attempt starts from a clean GetObject.
            self._sap_gui_auto = None
            self._application = None
            self._connection = None
            self._session = None
            return False

    def is_connected(self) -> bool:
        return self._connected

    def _find(self, path: str):
        if self._session is None:
            raise RuntimeError("Not connected to SAP GUI")
        return self._session.findById(path)

    def set_property(self, path, member, value):
        setattr(self._find(path), member, value)

    def call_method(self, path, member, args):
        element = self._find(path)
        method = getattr(element, member)
        if not args:
            return method()
        return method(*args)

    def get_property(self, path, member):
        return getattr(self._find(path), member)

    def get_session_property(self, member):
        if self._session is None:
            raise RuntimeError("Not connected to SAP GUI")
        obj = self._session
        for part in member.split("."):
            obj = getattr(obj, part)
        return obj

    def disconnect(self) -> None:
        """Drop every COM reference so SAP sees no scripting client attached
        -- that stops SAP's "scripting active" state and restores the native
        Windows file dialogs. The next run's first command re-attaches from
        scratch, like the Excel's per-macro ``Attach_Session``.

        For the real backend the scripting-engine handles go too: the running
        SAP GUI keeps its own session state, so a fresh ``GetObject("SAPGUI")``
        re-locates the same windows. The fake keeps its application graph so
        scenario state survives between runs (it has no real SAP to hold it).

        gc.collect() runs for the real backend so the dropped win32com
        CDispatch wrappers (mutually-referencing) are torn down now rather than
        lingering on the COM apartment.
        """
        self._session = None
        self._connection = None
        self._connected = False
        if self._app_factory is None:
            self._application = None
            self._sap_gui_auto = None
            gc.collect()
        logger.info("SAP attachment released")
