"""BCsys server client for SAP Import.

The single seam for every server call the Import window makes: fetching the
list, reading the upload flag, setting the upload flag, and downloading
attachments. Tests substitute a fake transport/downloader, or stand in a whole
fake client on the window.

There is deliberately no ``flag_removed``/``unflag`` here -- the API has no
unflag endpoint (see docs/decisions/0005-upload-flag-semantics.md), so the
client must not pretend one exists.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from .models import API_BASE

_log = logging.getLogger("sap_import")

# Never committed: per-machine credential, alongside the other private files.
_PRIVATE_AUTH_PATH = Path(__file__).parent.parent.parent / "private" / "bcsys_auth"


def _default_auth() -> str:
    """BCsys Authorization header: env first, then the untracked private file."""
    env = os.environ.get("BCSYS_AUTH")
    if env and env.strip():
        return env.strip()
    try:
        return _PRIVATE_AUTH_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class ImportApiClient:
    """Talks to the BCsys server for the Import window.

    ``transport(endpoint, data, method) -> dict | None`` performs the HTTP call
    and ``downloader(url, filepath) -> bool`` fetches one attachment; both
    default to real implementations and can be replaced in tests. ``auth`` is
    the fully-resolved Authorization header (the window supplies a
    config-overridden value); when omitted it falls back to the environment and
    the shipped default.
    """

    def __init__(
        self,
        base: str = API_BASE,
        auth: str | None = None,
        transport=None,
        downloader=None,
        report_status=None,
        retries: int = 3,
        retry_delay: float = 0.5,
    ):
        self.base = base
        self.auth = auth
        self._transport = transport or self._http
        self._uses_default_transport = transport is None
        self._downloader = downloader or self._http_download
        self._report = report_status
        self.retries = retries
        self.retry_delay = retry_delay

    def _resolve_auth(self) -> str:
        return self.auth or _default_auth()

    def _report_status(self, text: str, kind: str = "info") -> None:
        if self._report:
            self._report(text, kind)

    # ---- transport --------------------------------------------------------
    def call(self, endpoint: str, data: dict | None = None, method: str = "GET",
             report: bool = True):
        """One raw round-trip; the fake stands in here.

        ``report=False`` suppresses the error surfaced by the default HTTP
        transport. Injected transports ignore it (they own their reporting).
        """
        if not report and self._uses_default_transport:
            return self._http(endpoint, data, method, report=False)
        return self._transport(endpoint, data, method)

    def _http(self, endpoint: str, data: dict | None = None, method: str = "GET",
              report: bool = True):
        auth = self._resolve_auth()
        if not auth:
            if report:
                self._report_status(
                    "BCsys auth not configured "
                    "(set BCSYS_AUTH or private/bcsys_auth)",
                    "error",
                )
            return None
        url = f"{self.base}/{endpoint}"
        body = json.dumps(data).encode("utf-8") if data else None
        req = Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": auth,
                "User-Agent": "Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1)",
                "Accept": "*/*",
            },
            method=method,
        )
        try:
            with urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except URLError as e:
            detail = ""
            read = getattr(e, "read", None)
            if callable(read):
                try:
                    raw_detail = read()
                    if isinstance(raw_detail, bytes):
                        detail = raw_detail.decode("utf-8", errors="replace")[:300]
                except Exception:
                    pass
            if report:
                self._report_status(
                    f"API error ({endpoint}): {e}{' - ' + detail if detail else ''}", "error"
                )
            return None
        except (json.JSONDecodeError, OSError) as e:
            if report:
                self._report_status(f"API error ({endpoint}): {e}", "error")
            return None

    # ---- endpoints --------------------------------------------------------
    def get_list(self, table: str = "WO", pg: str = "WP1"):
        # POST is a probe: a rejection here is expected and must not flash a
        # status-bar error before the GET fallback succeeds.
        data = self.call("getList", {"table": table, "PG": pg}, "POST", report=False)
        if data is None:
            data = self.call("getList", {"table": table, "PG": pg}, "GET")
        return data

    def get_flag(self, row_id: str, table: str = "WO") -> int:
        """0/1 for a real answer, -1 once the retries are exhausted."""
        for attempt in range(self.retries):
            data = self.call(f"getFlag?table={table}&rowId={row_id}")
            if data is not None:
                try:
                    val = int(str(data).strip())
                    return val if val in (0, 1) else -1
                except (ValueError, TypeError):
                    return -1
            if attempt < self.retries - 1 and self.retry_delay:
                time.sleep(self.retry_delay * (attempt + 1))
        return -1

    def flag_added(self, row_id: str, table: str = "WO") -> bool:
        return self.call("flagAdded", {"table": table, "rowId": row_id}) is not None

    # ---- attachments ------------------------------------------------------
    def download(self, url: str, filepath: str) -> bool:
        return self._downloader(url, filepath)

    def _http_download(self, url: str, filepath: str) -> bool:
        # WinHTTP (Windows, auto-logon) first; urllib as fallback for mock/Linux.
        try:
            from win32com.client import Dispatch

            http = Dispatch("WinHTTP.WinHttpRequest.5.1")
            http.Open("GET", url, False)
            http.SetAutoLogonPolicy(1)
            http.SetRequestHeader(
                "User-Agent", "Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1)"
            )
            http.SetRequestHeader("Accept", "image/webp,image/apng,image/*,*/*;q=0.8")
            http.Send()
            if http.Status != 200:
                raise OSError(f"HTTP {http.Status}: {http.StatusText}")
            data = http.ResponseBody
            if not data:
                return False
            with open(filepath, "wb") as f:
                f.write(data.tobytes() if hasattr(data, "tobytes") else data)
            return True
        except Exception as win_e:
            _log.debug("WinHTTP download failed, trying urllib: %s - %s", url, win_e)
        try:
            os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
            req = Request(
                url,
                headers={
                    "User-Agent": "Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1)",
                    "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
                },
            )
            with urlopen(req, timeout=30) as resp, open(filepath, "wb") as out:
                out.write(resp.read())
            return True
        except Exception as e2:
            _log.warning("Download failed: %s - %s", url, e2)
            return False
