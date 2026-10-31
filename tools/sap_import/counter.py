"""Server pending counter shared by the toolbar poll and the Import window.

The list endpoint returns only un-flagged rows, so its length is the server's
pending count. The toolbar reads this every 2 minutes when no upload is running;
while auto upload runs, the window's tick records its list response here so the
poll reuses it without a second request.

Read-only: a poll never touches the local queue. A single guard lets only one
fetch (poll or upload) be in flight, so the two can never race.
"""

from __future__ import annotations

from collections.abc import Callable


class ImportCounter:
    def __init__(self, api=None):
        self._api = api
        self._busy = False
        self.count = 0
        self.known = False
        # Connection health of the most recent request: None = never tried,
        # True = reached the server, False = failed. The toolbar's combined
        # SAP indicator reads this.
        self.last_ok: bool | None = None
        # Fired after a fresh count lands, so the toolbar can update at once
        # instead of waiting up to two minutes for its next tick.
        self.on_change: Callable[[], None] | None = None

    def _notify(self):
        callback = self.on_change
        if callback is None:
            return
        try:
            callback()
        except Exception:
            pass

    def begin(self) -> bool:
        """Take the fetch guard. False when another fetch already holds it."""
        if self._busy:
            return False
        self._busy = True
        return True

    def end(self):
        self._busy = False

    def note_request(self, ok: bool):
        """Record whether the last server request reached the API."""
        self.last_ok = bool(ok)
        self._notify()

    def record(self, data_list) -> int:
        """Count a list response the caller already fetched (no request)."""
        self.count = len(data_list or [])
        self.known = True
        self.last_ok = True
        self._notify()
        return self.count

    def poll(self, table: str = "WO", pg: str = "WP1") -> int | None:
        """Fetch the list read-only and count it. None when guarded or failed."""
        if not self.begin():
            return None
        try:
            if self._api is None:
                return None
            result = self._api.get_list(table, pg)
            if result is None:
                self.last_ok = False
                self._notify()
                return None
            return self.record(result.get("data", []))
        finally:
            self.end()
