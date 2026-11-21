"""Planner tool window -- the lifted Command Planner board.

The board itself lives in ``ui/planner_board.py``. This module owns the tool
window, loads the local preview data, and will later replace the two placeholder
callbacks with the real IW38 load and Send Changes scripts.
"""

from __future__ import annotations

import json
from pathlib import Path

from ui import ui_styles
from ui.custom_widgets import BaseToolWindow
from ui.planner_board import (
    PLANNER_TEAMS,
    PLANNER_USTATUS,
    PlannerBoard,
)

PREVIEW_TECHS = {
    "WP-M-E": ["Jeffrey", "Dan", "Sofie", "Wim"],
    "WP-M-J": ["Jordan", "Pieter"],
    "WP-M-N": ["Niels", "Bram"],
    "WP-S": ["Sven", "Lars", "Mieke"],
    "EX-BELT": ["Eric", "Tom"],
}
PREVIEW_TITLES = [
    "Vervang pomp ME-01", "ATB aanvragen 4191447", "Controleer voorraad sealings",
    "Print werkorders week 45", "Revisie RR-2026-31", "Order 4001988 inspectie",
    "Leverancierslijst bijwerken", "ME5A export nakijken", "Site visit klant",
    "Lager trekrol vervangen", "Pitband L6 controleren", "Motor M3 uitlijnen",
    "Trillingsmeting wals W4", "Olie verversen tandwielkast", "Filter set bestellen",
    "Veiligheidscheck loopbrug", "Sensor niveau bijstellen", "Koppeling inspecteren",
    "Transportband spannen", "Pomp sealings vervangen",
]


def preview_tasks() -> list[dict]:
    """Synthetic 60-card board used until the IW38 load is wired."""
    day_pattern = [0, 0, 0, 0, 1, 1, 1, 2, 2, 3, 4, 4]
    priorities = ["1", "2", "3", "4"]
    tasks = []
    for i in range(60):
        team = PLANNER_TEAMS[i % len(PLANNER_TEAMS)]
        backlog = i % 9 == 0
        start = -1 if backlog else day_pattern[
            (i // len(PLANNER_TEAMS)) % len(day_pattern)]
        span = 1
        if not backlog and i % 8 == 0:
            span = 3 if i % 16 == 0 else 2
            span = max(1, min(span, 5 - start))
        techs = PREVIEW_TECHS.get(team, ["Alex", "Sam"])
        chosen = [techs[i % len(techs)]]
        if i % 5 == 0:
            chosen.append(techs[(i + 1) % len(techs)])
        tasks.append({
            "id": 1000 + i,
            "order": f"400{1200 + i}",
            "title": PREVIEW_TITLES[i % len(PREVIEW_TITLES)],
            "team": team,
            "start": start,
            "span": span,
            "priority": priorities[(i * 3) % 4],
            "ustatus": PLANNER_USTATUS[i % len(PLANNER_USTATUS)],
            "rev": "2026-W45" if i % 6 else "2026-W45-A",
            "floc": f"CLWA-P0{i % 7 + 1}-{i % 9 + 1:02d}",
            "techs": chosen,
            "rank": i,
            "rail_rank": i,
            "dirty": False,
            "sap_conflict": False,
        })
    return tasks


def _load_local_fixture() -> tuple[list[dict], list[str]]:
    seed_path = Path(__file__).resolve().parent.parent / "tmp_user" / "planner_seed.json"
    try:
        data = json.loads(seed_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    tasks = data.get("tasks") or []
    if tasks:
        return tasks, list(data.get("general_workplaces") or ["WP-G"])
    return preview_tasks(), ["WP-G"]


class PlannerWindow(BaseToolWindow):
    """Window hosting the day x team board."""

    def __init__(self, parent, engine, tasks=None, general_workplaces=None):
        super().__init__(
            parent,
            engine,
            titlebar_color=ui_styles.TOOL_COLORS.get("planner", ui_styles.SUPPORT_COLOR),
        )
        self.title("Planner")
        self.geometry("1500x860")
        self.minsize(1180, 720)

        if tasks is None:
            tasks, general_workplaces = _load_local_fixture()
        self.board = PlannerBoard(
            self,
            tasks=tasks,
            general_workplaces=general_workplaces,
            on_send=self._send_changes,
            on_reload=self._reload_sap,
        )
        self.board.pack(fill="both", expand=True)
        status_bar, _ = self._build_status_bar(
            "Reload SAP and Send changes are not wired yet"
        )
        status_bar.pack_configure(side="bottom")
        self._show()

    def _send_changes(self):
        dirty = self.board.dirty_tasks()
        if not dirty:
            self.report_status("Nothing staged to send", "info")
            return
        self.report_status(
            f"{len(dirty)} staged card(s); SAP send is not wired yet", "warning"
        )

    def _reload_sap(self):
        self.report_status("IW38 reload is not wired yet", "warning")
