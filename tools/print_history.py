"""PrintHistoryStore -- local JSON history of Print Orders runs.

Each run is one folder under ``data/print_output/`` holding the work-order
PDFs, attachment PDFs and optional merged PDF it produced:

    {"runs": [{
        "id": "2026-11-02_143000_3orders",
        "folder": "C:/.../data/print_output/2026-11-02_143000_3orders",
        "started": "02.11.2026 14:30",
        "started_day": "02.11.2026",
        "kind": "batch" | "single",
        "done": 2, "failed": 1,
        "merged": "merged_4001235-4001240.pdf",
        "orders": [{
            "order": "4001235", "short_text": "...", "status": "Done",
            "workorder": "WO_4001235_....pdf",
            "attachments": "WO_4001235_..._attachments.pdf",
            "error": ""
        }]
    }]}

Only the latest 30 run folders are kept; older ones are deleted together
with their history entries. Pure data logic, no UI.
"""

from __future__ import annotations

import json
import os
import shutil

DEFAULT_RUN_LIMIT = 30


def default_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "data", "print_history.json")


def default_output_root() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "data", "print_output")


def _inside(path: str, root: str) -> bool:
    try:
        path = os.path.normcase(os.path.abspath(path))
        root = os.path.normcase(os.path.abspath(root))
        return os.path.commonpath([path, root]) == root and path != root
    except (ValueError, OSError):
        return False


class PrintHistoryStore:
    """Loads/saves the history JSON. Every mutator persists immediately;
    I/O failures never raise -- history must not break the tool window."""

    def __init__(self, path: str | None = None, output_root: str | None = None):
        self.path = path or default_path()
        self.output_root = output_root or default_output_root()
        self._data: dict = {"runs": []}
        self.load()

    # ---- persistence -------------------------------------------------
    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("runs"), list):
                self._data = data
        except (OSError, ValueError):
            self._data = {"runs": []}

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    @property
    def runs(self) -> list[dict]:
        return self._data["runs"]

    # ---- mutators ----------------------------------------------------
    def add_run(self, run: dict) -> dict:
        """Insert a finished run newest-first and persist it."""
        self._data["runs"].insert(0, run)
        self.save()
        return run

    def update_run(self, run_id: str, **changes) -> None:
        for run in self._data["runs"]:
            if run.get("id") == run_id:
                run.update(changes)
                self.save()
                return

    def prune(self, keep: int = DEFAULT_RUN_LIMIT) -> list[dict]:
        """Drop runs beyond ``keep`` and delete their folders.

        Only folders recorded in this history are removed, and only when they
        live under ``output_root``. Returns the dropped run entries.
        """
        runs = self._data["runs"]
        if keep < 0:
            keep = 0
        kept, dropped = runs[:keep], runs[keep:]
        if not dropped:
            return []
        self._data["runs"] = kept
        for run in dropped:
            self._remove_folder(run.get("folder", ""))
        self.save()
        return dropped

    def _remove_folder(self, folder: str) -> None:
        if not folder or not _inside(folder, self.output_root):
            return
        shutil.rmtree(folder, ignore_errors=True)

    # ---- lookups -----------------------------------------------------
    def latest_run(self) -> dict | None:
        return self._data["runs"][0] if self._data["runs"] else None

    def find_order_latest(self, order: str) -> tuple[dict, dict] | None:
        order = (order or "").strip()
        if not order:
            return None
        for run in self._data["runs"]:
            for entry in run.get("orders", []) or []:
                if str(entry.get("order", "")).strip() == order:
                    return run, entry
        return None

    def pdf_path(self, order: str, kind: str = "workorder") -> str | None:
        """Absolute path to the newest file for ``order`` and ``kind``."""
        found = self.find_order_latest(order)
        if found is None:
            return None
        run, entry = found
        name = entry.get(kind) or ""
        if not name:
            return None
        path = os.path.join(run.get("folder", ""), name)
        return path if os.path.exists(path) else None
