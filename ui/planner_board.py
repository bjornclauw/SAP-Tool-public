"""Planner board -- the Command Planner day x team surface.

Lifted from prototype variant D (see
``work/planner-tool/prototype/prototype_planner_ctk.py``). The Canvas drawing,
geometry, drag/resize, drop preview, general-backlog rail, collapsed lanes and
Week/Day modes are carried over. Data is supplied by the caller; SAP loading,
persistence and change sending are wired by the Planner tool window.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable, Iterable

import customtkinter as ctk

# Keep CustomTkinter from switching the process to per-monitor DPI awareness
# when it creates a popup: Tk 8.6 cannot rescale on DPI changes, so that would
# resize every window (the docked toolbar included) on a monitor move.
ctk.deactivate_automatic_dpi_awareness()

PRIORITY_BY_CODE = {"1": "#ff6b7a", "2": "#f4b860", "3": "#8ab4f8", "4": "#54c6a3"}
PLANNER_TEAMS = ["WP-M-E", "WP-M-J", "WP-M-N", "WP-S", "WP-E-J",
                 "EX-BELT", "EX-BELTZ", "EX-KV", "EX-VERM", "WP-G"]
PLANNER_DAY_LABELS = [("Mon", "10"), ("Tue", "11"), ("Wed", "12"),
                      ("Thu", "13"), ("Fri", "14")]
PLANNER_USTATUS = ["10CR", "20WA", "30PR", "40WM", "50RS", "60RE", "70IE", "80EV"]

LIGHT_BG = "#f2f4f8"
LIGHT_PANEL = "#ffffff"
LIGHT_BORDER = "#e2e6ee"
LIGHT_TEXT = "#1a1f2b"
LIGHT_MUTED = "#7b8496"

_PRIORITY_NAMES = {"High": "1", "Medium": "2", "Low": "4"}


def normalise_tasks(tasks: Iterable[object] | None) -> list[dict]:
    """Return cards with every field the board draws, tolerating bad input."""
    result: list[dict] = []
    used_ids: set[int] = set()
    for index, raw in enumerate(tasks or []):
        if not isinstance(raw, dict):
            continue
        order = str(raw.get("order") or "").strip()
        title = str(raw.get("title") or "").strip()
        if not order and not title:
            continue
        order = order or title

        try:
            task_id = int(raw.get("id") or 0)
        except (TypeError, ValueError):
            task_id = 0
        if task_id <= 0 or task_id in used_ids:
            task_id = max(used_ids, default=0) + 1
        used_ids.add(task_id)

        try:
            start = int(raw.get("start", -1))
        except (TypeError, ValueError):
            start = -1
        if start < -1 or start > 4:
            start = -1
        try:
            span = int(raw.get("span", 1))
        except (TypeError, ValueError):
            span = 1
        span = max(1, min(span, 5 - start)) if start >= 0 else 1

        priority = str(raw.get("priority") or "2")
        priority = _PRIORITY_NAMES.get(priority, priority)
        if priority not in PRIORITY_BY_CODE:
            priority = "2"

        team = str(raw.get("team") or PLANNER_TEAMS[0])
        try:
            rank = int(raw.get("rank", index))
        except (TypeError, ValueError):
            rank = index
        try:
            rail_rank = int(raw.get("rail_rank", rank))
        except (TypeError, ValueError):
            rail_rank = rank

        result.append({
            "id": task_id,
            "order": order,
            "title": title or order,
            "team": team,
            "start": start,
            "span": span,
            "priority": priority,
            "ustatus": str(raw.get("ustatus") or PLANNER_USTATUS[0]),
            "rev": str(raw.get("rev") or ""),
            "floc": str(raw.get("floc") or ""),
            "techs": [str(name) for name in raw.get("techs") or []],
            "rank": rank,
            "rail_rank": rail_rank,
            "dirty": bool(raw.get("dirty")),
            "sap_conflict": bool(raw.get("sap_conflict")),
            "sap_field": str(raw.get("sap_field") or ""),
            "sap_was": str(raw.get("sap_was") or ""),
            "sap_value": str(raw.get("sap_value") or ""),
        })
    return result


class PlannerBoard(ctk.CTkFrame):
    """Day columns x team swimlanes with a general-backlog rail."""

    D_GUTTER = 164
    D_BACKLOG = 148
    D_HEADER = 56
    D_LANE = 190
    D_LANE_COLLAPSED = 56
    D_CAP = 3
    D_CARD_H = 42
    D_RAIL_HEADER = 54
    D_RAIL_CARD_H = 64
    D_RAIL_SLOT = 70

    def __init__(self, master, tasks: Iterable[dict] | None = None,
                 general_workplaces: Iterable[str] | None = None,
                 on_send: Callable[[], None] | None = None,
                 on_reload: Callable[[], None] | None = None):
        super().__init__(master, fg_color=LIGHT_BG, corner_radius=0)
        self._on_send = on_send
        self._on_reload = on_reload
        self.planner_tasks = normalise_tasks(tasks)
        self._d_general = set(general_workplaces or ("WP-G",))
        self._d_selected = None
        self._d_mode = "week"
        self._d_day = 2
        self._d_preview = []
        self._d_backlog_folded = False
        self._d_rail_open = False
        self._d_redraw_job = None
        self._d_rail_offset = 0
        self._d_rail_maxoff = 0
        self._d_rail_hits = []
        self._d_rail_overlay = []
        self._d_offset = 0
        self._d_maxoff = 0
        self._d_xoffset = 0
        self._d_maxxoff = 0
        self._d_collapsed = set()
        self._d_team_filter = "All teams"
        self._d_priority_filter = "All priorities"
        self._d_drag = None
        self._d_hits = []
        self._d_ghost = []
        self._d_canvas = None
        self._d_send_btn = None

        self.font_xxxl = ctk.CTkFont("Segoe UI", 26, "bold")
        self.font_xxl = ctk.CTkFont("Segoe UI", 19, "bold")
        self.font_xl = ctk.CTkFont("Segoe UI", 15, "bold")
        self.font_l = ctk.CTkFont("Segoe UI", 13, "bold")
        self.font_m = ctk.CTkFont("Segoe UI", 12)
        self.font_s = ctk.CTkFont("Segoe UI", 11)
        self.font_xs = ctk.CTkFont("Segoe UI", 10, "bold")
        self._d_font_bold = tkfont.Font(family="Segoe UI", size=9,
                                        weight="bold")
        self._d_font_small = tkfont.Font(family="Segoe UI", size=6)
        self._d_font_title = tkfont.Font(family="Segoe UI", size=8)

        self.stage = ctk.CTkFrame(self, fg_color="transparent")
        self.stage.pack(fill="both", expand=True)
        self._build()


    def _d_teams(self):
        present = {task["team"] for task in self.planner_tasks}
        general = {team for team in self._d_general if team in present}
        ordered = [team for team in PLANNER_TEAMS
                   if team in present and team not in general]
        ordered += sorted(present - set(ordered) - general)
        return ordered

    def _d_days(self):
        return list(range(5)) if self._d_mode == "week" else [self._d_day]

    def _d_backlog_w(self):
        return 40 if self._d_backlog_folded else self.D_BACKLOG

    def _d_days_x(self):
        return self.D_GUTTER + self._d_backlog_w()

    def _d_floc(self, task):
        parts = [p for p in (task.get("floc") or "").split("-") if p]
        return "-".join(parts[:4])

    def _build(self):
        shell = ctk.CTkFrame(self.stage, fg_color=LIGHT_BG, corner_radius=0)
        shell.pack(fill="both", expand=True)

        top = ctk.CTkFrame(shell, fg_color=LIGHT_PANEL, corner_radius=0,
                           height=70)
        top.pack(fill="x")
        top.pack_propagate(False)

        title = ctk.CTkFrame(top, fg_color="transparent")
        title.pack(side="left", padx=22, pady=8)
        ctk.CTkLabel(title, text="Planner", text_color=LIGHT_TEXT,
                     font=self.font_xxl).pack(anchor="w")
        self._d_subtitle = ctk.CTkLabel(
            title, text="", text_color=LIGHT_MUTED, font=self.font_s,
        )
        self._d_subtitle.pack(anchor="w")

        filters = ctk.CTkFrame(top, fg_color="transparent")
        filters.pack(side="left", padx=20)
        ctk.CTkOptionMenu(
            filters, values=["All teams"] + self._d_teams(), width=124,
            height=30, font=self.font_s, fg_color="#eef1f6",
            button_color="#dde2ea", text_color=LIGHT_TEXT,
            dropdown_fg_color=LIGHT_PANEL, dropdown_text_color=LIGHT_TEXT,
            command=self._d_set_team,
        ).pack(side="left", padx=3)
        ctk.CTkOptionMenu(
            filters, values=["All priorities", "1", "2", "3", "4"],
            width=122, height=30, font=self.font_s, fg_color="#eef1f6",
            button_color="#dde2ea", text_color=LIGHT_TEXT,
            dropdown_fg_color=LIGHT_PANEL, dropdown_text_color=LIGHT_TEXT,
            command=self._d_set_priority,
        ).pack(side="left", padx=3)

        mode = ctk.CTkFrame(top, fg_color="transparent")
        mode.pack(side="left", padx=(10, 0))
        self._d_mode_btns = {}
        for label, value in (("Week", "week"), ("Day", "day")):
            button = ctk.CTkButton(
                mode, text=label, width=52, height=30, corner_radius=8,
                font=self.font_s, fg_color="#eef1f6",
                hover_color="#dde2ea", text_color=LIGHT_MUTED,
                command=lambda v=value: self._d_set_mode(v),
            )
            button.pack(side="left", padx=2)
            self._d_mode_btns[value] = button
        ctk.CTkButton(
            mode, text="\u2039", width=26, height=30, corner_radius=8,
            font=self.font_l, fg_color="#eef1f6", hover_color="#dde2ea",
            text_color=LIGHT_TEXT, command=lambda: self._d_shift_day(-1),
        ).pack(side="left", padx=(8, 2))
        self._d_day_label = ctk.CTkLabel(
            mode, text="", width=72, text_color=LIGHT_TEXT, font=self.font_s)
        self._d_day_label.pack(side="left")
        ctk.CTkButton(
            mode, text="\u203a", width=26, height=30, corner_radius=8,
            font=self.font_l, fg_color="#eef1f6", hover_color="#dde2ea",
            text_color=LIGHT_TEXT, command=lambda: self._d_shift_day(1),
        ).pack(side="left", padx=(2, 0))

        self._d_send_btn = ctk.CTkButton(
            top, text="Send changes", width=142, height=34, corner_radius=8,
            fg_color="#2f6fd0", hover_color="#255cb0",
            font=self.font_l, command=self._d_send,
        )
        self._d_send_btn.pack(side="right", padx=(0, 20))
        ctk.CTkButton(
            top, text="Reload SAP", width=100, height=30, corner_radius=8,
            fg_color="#eef1f6", hover_color="#dde2ea",
            text_color=LIGHT_TEXT, font=self.font_s,
            command=self._d_reload,
        ).pack(side="right", padx=(0, 8))
        self._d_update_mode_buttons()

        content = ctk.CTkFrame(shell, fg_color=LIGHT_BG, corner_radius=0)
        content.pack(fill="both", expand=True, padx=18, pady=14)

        self._build_d_detail(content)

        self._d_rail = ctk.CTkFrame(
            content, width=44, fg_color=LIGHT_PANEL, corner_radius=12,
            border_width=1, border_color=LIGHT_BORDER)
        self._d_rail.pack(side="left", fill="y", padx=(0, 10))
        self._d_rail.pack_propagate(False)
        self._d_rail.grid_propagate(False)
        self._d_rail.grid_rowconfigure(0, weight=1)
        self._d_rail.grid_columnconfigure(0, weight=1)
        self._d_rail_canvas = tk.Canvas(
            self._d_rail, bg="#fffaf0", highlightthickness=0, bd=0)
        self._d_rail_canvas.grid(row=0, column=0, sticky="nsew",
                                 padx=1, pady=1)
        self._d_rail_scroll = ctk.CTkScrollbar(
            self._d_rail, command=self._d_rail_scroll_cmd,
            button_color="#e4d3b3", button_hover_color="#d6bf94")
        self._d_rail_canvas.bind("<Configure>",
                                 lambda _e: self._d_rail_redraw())
        self._d_rail_canvas.bind("<ButtonPress-1>", self._d_rail_press)
        self._d_rail_canvas.bind("<B1-Motion>", self._d_rail_motion)
        self._d_rail_canvas.bind("<ButtonRelease-1>", self._d_rail_release)
        self._d_rail_canvas.bind("<MouseWheel>", self._d_rail_wheel)
        self._d_render_rail()

        board = ctk.CTkFrame(content, fg_color=LIGHT_PANEL, corner_radius=12,
                             border_width=1, border_color=LIGHT_BORDER)
        board.pack(side="left", fill="both", expand=True)
        board.grid_rowconfigure(0, weight=1)
        board.grid_columnconfigure(0, weight=1)

        self._d_sb = ctk.CTkScrollbar(board, command=self._d_scroll,
                                      button_color="#dfe4ec",
                                      button_hover_color="#c9d1dd")
        self._d_sb.grid(row=0, column=1, sticky="ns", padx=(0, 4),
                        pady=(6, 0))

        self._d_hsb = ctk.CTkScrollbar(board, orientation="horizontal",
                                       command=self._d_scroll_x,
                                       button_color="#dfe4ec",
                                       button_hover_color="#c9d1dd")
        self._d_hsb.grid(row=1, column=0, sticky="ew", padx=(6, 0),
                         pady=(0, 4))

        self._d_canvas = tk.Canvas(board, bg=LIGHT_BG, highlightthickness=0,
                                   bd=0)
        self._d_canvas.grid(row=0, column=0, sticky="nsew", padx=(6, 0),
                            pady=(6, 0))
        self._d_canvas.bind("<Configure>", lambda _e: self._d_schedule_redraw())
        self._d_canvas.bind("<ButtonPress-1>", self._d_press)
        self._d_canvas.bind("<B1-Motion>", self._d_motion)
        self._d_canvas.bind("<ButtonRelease-1>", self._d_release)
        self._d_canvas.bind("<MouseWheel>", self._d_wheel)
        self._d_canvas.bind("<Motion>", self._d_hover)
        self._d_initial_redraw = self.after(40, self._d_redraw)
        self._d_update_send()

    def _build_d_detail(self, content):
        panel = ctk.CTkFrame(content, width=276, fg_color=LIGHT_PANEL,
                             corner_radius=12, border_width=1,
                             border_color=LIGHT_BORDER)
        panel.pack(side="right", fill="y", padx=(14, 0))
        panel.pack_propagate(False)
        self._d_detail_panel = panel
        self._d_render_detail()

    def _d_task(self, task_id):
        for task in self.planner_tasks:
            if task["id"] == task_id:
                return task
        return None

    def _d_field(self, parent, label, value):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=2)
        ctk.CTkLabel(row, text=label, width=94, anchor="w",
                     text_color=LIGHT_MUTED, font=self.font_s).pack(side="left")
        ctk.CTkLabel(row, text=value, anchor="w", text_color=LIGHT_TEXT,
                     font=self.font_s, justify="left", wraplength=140
                     ).pack(side="left", fill="x", expand=True)

    def _d_date(self, start):
        if start < 0:
            return "Backlog"
        name, date = PLANNER_DAY_LABELS[start]
        return f"{name} {date}"

    def _d_stop(self, task):
        if task["start"] < 0:
            return "\u2014"
        end = min(4, task["start"] + task["span"] - 1)
        name, date = PLANNER_DAY_LABELS[end]
        return f"{name} {date}"

    def _d_render_detail(self):
        panel = getattr(self, "_d_detail_panel", None)
        if panel is None:
            return
        for child in panel.winfo_children():
            child.destroy()
        ctk.CTkLabel(panel, text="CARD DETAIL", text_color=LIGHT_MUTED,
                     font=self.font_xs).pack(anchor="w", padx=16,
                                            pady=(16, 4))
        task = self._d_task(self._d_selected) if self._d_selected else None
        if task is None:
            ctk.CTkLabel(panel, text="Select a card", text_color=LIGHT_TEXT,
                         font=self.font_l).pack(anchor="w", padx=16)
            ctk.CTkLabel(
                panel,
                text=("Drag a card to a day and team, or click one to "
                      "inspect its SAP fields.\n\nAmber border = staged. "
                      "Red border = SAP changed since load."),
                text_color=LIGHT_MUTED, font=self.font_s, justify="left",
                wraplength=230,
            ).pack(anchor="w", padx=16, pady=(4, 0))
            ctk.CTkFrame(panel, fg_color="transparent").pack(
                fill="both", expand=True)
            return

        ctk.CTkLabel(panel, text=task["order"], text_color=LIGHT_MUTED,
                     font=self.font_s).pack(anchor="w", padx=16)
        ctk.CTkLabel(panel, text=task["title"], text_color=LIGHT_TEXT,
                     font=self.font_xl, justify="left", anchor="w",
                     wraplength=230).pack(fill="x", padx=16, pady=(2, 6))
        if task.get("sap_conflict"):
            field = task.get("sap_field", "field")
            was = task.get("sap_was", "?")
            value = task.get("sap_value", "?")
            ctk.CTkLabel(
                panel,
                text=(f"SAP changed silently: {field} {was} \u2192 {value}. "
                      "Your board value is written on Send."),
                text_color="#d13438", font=self.font_xs, justify="left",
                wraplength=230,
            ).pack(anchor="w", padx=16, pady=(0, 6))
        if task.get("dirty"):
            ctk.CTkLabel(panel, text="Staged \u2014 written on Send.",
                         text_color="#b06a1f", font=self.font_xs
                         ).pack(anchor="w", padx=16, pady=(0, 6))

        self._d_field(panel, "Team", task["team"])
        self._d_field(panel, "Board order",
                      str(task.get("rank", 0) + 1))
        self._d_field(panel, "Planned start", self._d_date(task["start"]))
        self._d_field(panel, "Planned stop", self._d_stop(task))
        self._d_field(panel, "Priority", task["priority"])
        self._d_field(panel, "User status", task["ustatus"])
        self._d_field(panel, "Revision", task["rev"])
        self._d_field(panel, "Functional loc.", task["floc"] or "\u2014")
        self._d_field(panel, "Technicians", ", ".join(task["techs"]) or "None")
        if task["team"] in self._d_general and task["start"] < 0:
            ctk.CTkLabel(
                panel,
                text=("This workplace is treated as the general backlog. "
                      "Drag it onto a day to plan it."),
                text_color="#b06a1f", font=self.font_xs, wraplength=230,
                justify="left",
            ).pack(anchor="w", padx=16, pady=(6, 0))
        ctk.CTkLabel(
            panel,
            text="Technicians are board-only; SAP gets the team.",
            text_color="#98a2b3", font=self.font_xs, wraplength=230,
            justify="left",
        ).pack(anchor="w", padx=16, pady=(6, 0))
        ctk.CTkFrame(panel, fg_color="transparent").pack(
            fill="both", expand=True)

    def _d_rail_tasks(self):
        return sorted(
            [t for t in self.planner_tasks if t["team"] in self._d_general],
            key=lambda t: (t["start"] >= 0,
                           t.get("rail_rank", t.get("rank", 0)),
                           t["order"]))

    def _d_rail_toggle(self, _event=None):
        self._d_rail_open = not self._d_rail_open
        self._d_rail_clear_overlay()
        self._d_render_rail()

    def _d_render_rail(self):
        rail = getattr(self, "_d_rail", None)
        canvas = getattr(self, "_d_rail_canvas", None)
        if rail is None or canvas is None:
            return
        try:
            if not rail.winfo_exists() or not canvas.winfo_exists():
                return
        except Exception:
            return
        rail.configure(width=278 if self._d_rail_open else 44)
        scroll = getattr(self, "_d_rail_scroll", None)
        if scroll is not None:
            try:
                if self._d_rail_open:
                    scroll.grid(row=0, column=1, sticky="ns", pady=4,
                                padx=(0, 2))
                else:
                    scroll.grid_remove()
            except Exception:
                pass
        if not self._d_rail_open:
            self._d_rail_offset = 0
        self._d_rail_redraw()

    def _d_rail_redraw(self):
        canvas = getattr(self, "_d_rail_canvas", None)
        if canvas is None:
            return
        try:
            if not canvas.winfo_exists():
                return
        except Exception:
            return
        width = max(canvas.winfo_width(), 20)
        height = max(canvas.winfo_height(), 20)
        tasks = self._d_rail_tasks()
        canvas.configure(bg="#fffaf0")
        canvas.delete("all")
        self._d_rail_hits = []
        self._d_rail_overlay = []
        if not self._d_rail_open:
            badge_w = 26
            self._d_round_on(canvas, (width - badge_w) / 2, 14,
                             (width + badge_w) / 2, 40, 8,
                             fill="#b07a1f", outline="")
            canvas.create_text(width / 2, 27, text=str(len(tasks)),
                               anchor="center", fill="#ffffff",
                               font=("Segoe UI", 9, "bold"))
            canvas.create_text(width / 2, height / 2,
                               text="GENERAL BACKLOG", anchor="center",
                               fill="#9a6212", angle=90,
                               font=("Segoe UI", 8, "bold"))
            self._d_rail_hit((0, 0, width, height, "toggle", None))
            self._d_rail_set_scroll(0, 1)
            return

        header_h = self.D_RAIL_HEADER
        canvas.create_rectangle(0, 0, width, header_h,
                                fill="#fdf3df", outline="")
        canvas.create_line(0, header_h, width, header_h,
                           fill="#f0d9a8")
        canvas.create_text(10, 16, text="\u25c0", anchor="nw",
                           fill="#9a6212", font=("Segoe UI", 10, "bold"))
        canvas.create_text(30, 12, text="General backlog", anchor="nw",
                           fill="#7a4a08", font=("Segoe UI", 11, "bold"))
        canvas.create_text(30, 31, text=f"{len(tasks)} cards", anchor="nw",
                           fill="#b07a1f", font=("Segoe UI", 8))
        canvas.create_text(width - 16, 16, text="\u00b7\u00b7\u00b7",
                           anchor="ne", fill="#9a6212",
                           font=("Segoe UI", 12, "bold"))
        self._d_rail_hit((0, 0, 28, header_h, "toggle", None))
        self._d_rail_hit((width - 34, 0, width, header_h, "settings", None))

        slot = self.D_RAIL_SLOT
        card_h = self.D_RAIL_CARD_H
        view = max(1, height - header_h)
        content_h = len(tasks) * slot + 8
        self._d_rail_maxoff = max(0, content_h - view)
        if self._d_rail_offset > self._d_rail_maxoff:
            self._d_rail_offset = self._d_rail_maxoff
        if self._d_rail_offset < 0:
            self._d_rail_offset = 0
        if not tasks:
            canvas.create_text(width / 2, header_h + 30,
                               text="No cards in the\ngeneral backlog",
                               anchor="center", fill="#b3bccb",
                               justify="center", font=("Segoe UI", 9))
        for index, task in enumerate(tasks):
            y1 = header_h + 6 + index * slot - self._d_rail_offset
            if y1 + card_h < header_h or y1 > height:
                continue
            self._d_rail_card(6, y1, width - 18, card_h, task)
        if self._d_rail_maxoff:
            first = self._d_rail_offset / self._d_rail_maxoff
            last = min(1.0, first + view / max(1, content_h))
        else:
            first, last = 0.0, 1.0
        self._d_rail_set_scroll(first, last)

    def _d_rail_set_scroll(self, first, last):
        scroll = getattr(self, "_d_rail_scroll", None)
        if scroll is None:
            return
        try:
            scroll.set(first, last)
        except Exception:
            pass

    def _d_round_on(self, canvas, x1, y1, x2, y2, radius, **kw):
        points = [
            x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
            x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        ]
        return canvas.create_polygon(points, smooth=True, **kw)

    def _d_rail_card(self, x1, y1, width, height, task):
        canvas = self._d_rail_canvas
        selected = task["id"] == self._d_selected
        dirty = bool(task.get("dirty"))
        conflict = bool(task.get("sap_conflict"))
        if conflict:
            outline, fill, thick = "#d13438", "#fff7f7", 2
        elif dirty:
            outline, fill, thick = "#d99b2b", "#fffdf5", 2
        elif selected:
            outline, fill, thick = "#2f6fd0", "#eef4ff", 2
        else:
            outline, fill, thick = "#f0d9a8", "#ffffff", 1
        self._d_round_on(canvas, x1, y1, x1 + width, y1 + height, 8,
                         fill=fill, outline=outline, width=thick)
        canvas.create_rectangle(x1 + 1, y1 + 7, x1 + 5, y1 + height - 7,
                                fill=PRIORITY_BY_CODE.get(
                                    task["priority"], "#f4b860"),
                                outline="")
        order_w = self._d_font_bold.measure(task["order"]) + 8
        canvas.create_text(x1 + 11, y1 + 6, text=task["order"], anchor="nw",
                           fill=LIGHT_TEXT, font=("Segoe UI", 9, "bold"))
        floc = self._d_floc(task)
        room = width - order_w - 12
        if floc and room > 18:
            canvas.create_text(
                x1 + 11 + order_w, y1 + 7,
                text=self._d_fit(floc, self._d_font_small, room),
                anchor="nw", fill="#9a6212", font=("Segoe UI", 6))
        canvas.create_text(x1 + 11, y1 + 23,
                           text=self._d_fit(task["title"],
                                            self._d_font_title, width - 20),
                           anchor="nw", fill="#4a5363",
                           font=("Segoe UI", 8))
        canvas.create_text(x1 + 11, y1 + 41,
                           text=f"{task['team']} \u00b7 {self._d_date(task['start'])}",
                           anchor="nw", fill=LIGHT_MUTED,
                           font=("Segoe UI", 8))
        if conflict or dirty:
            state = "#d13438" if conflict else "#d99b2b"
            canvas.create_rectangle(x1 + width - 4, y1 + 6,
                                    x1 + width - 1, y1 + height - 6,
                                    fill=state, outline="")
        self._d_rail_hit((x1, y1, x1 + width, y1 + height,
                          "card", task["id"]))

    def _d_rail_hit(self, item):
        self._d_rail_hits.append(item)

    def _d_rail_at(self, x, y):
        for (x1, y1, x2, y2, kind, data) in reversed(self._d_rail_hits):
            if x1 <= x <= x2 and y1 <= y <= y2:
                return kind, data
        return None, None

    def _d_rail_index_at_y(self, y):
        if not self._d_rail_open:
            return 0
        tasks = self._d_rail_tasks()
        header_h = self.D_RAIL_HEADER
        slot = self.D_RAIL_SLOT
        for index in range(len(tasks)):
            centre = (header_h + 6 + index * slot - self._d_rail_offset
                      + self.D_RAIL_CARD_H / 2)
            if y < centre:
                return index
        return len(tasks)

    def _d_rail_index_from_root(self, y_root):
        canvas = getattr(self, "_d_rail_canvas", None)
        if canvas is None:
            return 0
        try:
            return self._d_rail_index_at_y(y_root - canvas.winfo_rooty())
        except Exception:
            return 0

    def _d_rail_press(self, event):
        kind, data = self._d_rail_at(event.x, event.y)
        if kind == "toggle":
            self._d_rail_toggle()
            return
        if kind == "settings":
            self._d_general_settings()
            return
        if kind != "card":
            return
        task = self._d_task(data)
        if task is None:
            return
        self._d_selected = data
        self._d_render_detail()
        self._d_redraw()
        self._d_drag = {"kind": "rail", "id": data,
                        "x": event.x_root, "y": event.y_root,
                        "moved": False}

    def _d_rail_motion(self, event):
        drag = self._d_drag
        if not drag or drag.get("kind") != "rail":
            return
        if abs(event.x_root - drag["x"]) + abs(event.y_root - drag["y"]) > 6:
            drag["moved"] = True
        task = self._d_task(drag["id"])
        if task is None:
            return
        rel = self._d_canvas_rel(event.x_root, event.y_root)
        if rel is not None:
            self._d_rail_clear_overlay()
            self._d_board_ghost(rel[0], rel[1], task)
            self._d_draw_drop_preview(rel[0], rel[1], task)
            return
        self._d_clear_board_ghost()
        self._d_clear_preview()
        if (self._d_rail_open
                and self._d_over_rail(event.x_root, event.y_root)):
            self._d_rail_draw_overlay(event.x_root, event.y_root, task)
        else:
            self._d_rail_clear_overlay()

    def _d_rail_release(self, event):
        drag = self._d_drag
        if not drag or drag.get("kind") != "rail":
            return
        self._d_drag = None
        self._d_rail_clear_overlay()
        self._d_clear_board_ghost()
        self._d_clear_preview()
        task = self._d_task(drag["id"])
        if task is None:
            return
        if not drag["moved"]:
            return
        if self._d_over_rail(event.x_root, event.y_root):
            self._d_rail_insert(task, self._d_rail_index_from_root(
                event.y_root))
            return
        rel = self._d_canvas_rel(event.x_root, event.y_root)
        if rel is not None:
            target = self._d_drop_target(rel[0], rel[1])
            if target is not None:
                self._d_apply_drop(task, *target)
                return
        self._d_redraw()

    def _d_rail_clear_overlay(self):
        canvas = getattr(self, "_d_rail_canvas", None)
        for item in getattr(self, "_d_rail_overlay", []):
            try:
                canvas.delete(item)
            except Exception:
                pass
        self._d_rail_overlay = []

    def _d_rail_draw_overlay(self, x_root, y_root, task):
        canvas = getattr(self, "_d_rail_canvas", None)
        if canvas is None or not self._d_rail_open:
            return
        self._d_rail_clear_overlay()
        try:
            x = x_root - canvas.winfo_rootx()
            y = y_root - canvas.winfo_rooty()
        except Exception:
            return
        width = max(canvas.winfo_width(), 20)
        index = self._d_rail_index_at_y(y)
        line_y = (self.D_RAIL_HEADER + 6 + index * self.D_RAIL_SLOT
                  - self._d_rail_offset - 3)
        self._d_rail_overlay.append(canvas.create_line(
            6, line_y, width - 12, line_y, fill="#2f6fd0", width=3))
        ghost_w = max(120, width - 24)
        gx = max(4, min(x + 10, width - ghost_w - 4))
        gy = max(self.D_RAIL_HEADER + 2, y + 8)
        self._d_rail_overlay.append(self._d_round_on(
            canvas, gx, gy, gx + ghost_w, gy + 36, 7, fill="#ffffff",
            outline="#2f6fd0", width=2))
        self._d_rail_overlay.append(canvas.create_text(
            gx + 9, gy + 18,
            text=self._d_truncate(task["order"] + "  " + task["title"],
                                  ghost_w - 18),
            anchor="w", fill=LIGHT_TEXT, font=("Segoe UI", 8)))

    def _d_rail_insert(self, task, index):
        tasks = self._d_rail_tasks()
        current = next((i for i, item in enumerate(tasks) if item is task),
                       None)
        if current is not None and current < index:
            index -= 1
        tasks = [item for item in tasks if item is not task]
        index = max(0, min(index, len(tasks)))
        tasks.insert(index, task)
        if task["team"] not in self._d_general:
            if not self._d_general:
                return
            task["team"] = sorted(self._d_general)[0]
        task["start"] = -1
        task["span"] = 1
        task["dirty"] = True
        for rank, item in enumerate(tasks):
            item["rail_rank"] = rank
        self._d_rail_clear_overlay()
        self._d_render_detail()
        self._d_update_send()
        self._d_render_rail()
        self._d_redraw()

    def _d_rail_scroll_cmd(self, *args):
        if not args:
            return
        if args[0] == "moveto":
            self._d_rail_offset = int(float(args[1]) * self._d_rail_maxoff)
        elif args[0] == "scroll":
            amount = int(args[1])
            step = 50 if (len(args) < 3 or args[2] != "pages") else 300
            self._d_rail_offset += amount * step
        self._d_rail_redraw()

    def _d_rail_wheel(self, event):
        if not self._d_rail_open:
            return
        self._d_rail_offset -= int(event.delta / 120) * 50
        self._d_rail_redraw()

    def _d_clear_board_ghost(self):
        if self._d_canvas is None:
            return
        for item in self._d_ghost:
            self._d_canvas.delete(item)
        self._d_ghost = []

    def _d_board_ghost(self, x, y, task):
        self._d_clear_board_ghost()
        self._d_ghost.append(self._d_round(
            x + 12, y + 10, x + 162, y + 44, 7, fill="#ffffff",
            outline="#2f6fd0", width=2))
        self._d_ghost.append(self._d_canvas.create_text(
            x + 21, y + 27,
            text=self._d_truncate(task["order"] + "  " + task["title"], 138),
            anchor="w", fill=LIGHT_TEXT, font=("Segoe UI", 8)))
    def _d_canvas_rel(self, x_root, y_root):
        canvas = self._d_canvas
        if canvas is None:
            return None
        try:
            x = x_root - canvas.winfo_rootx()
            y = y_root - canvas.winfo_rooty()
            if 0 <= x < canvas.winfo_width() and 0 <= y < canvas.winfo_height():
                return x, y
        except Exception:
            return None
        return None

    def _d_over_rail(self, x_root, y_root):
        rail = getattr(self, "_d_rail", None)
        if rail is None:
            return False
        try:
            return (rail.winfo_rootx() <= x_root
                    <= rail.winfo_rootx() + rail.winfo_width()
                    and rail.winfo_rooty() <= y_root
                    <= rail.winfo_rooty() + rail.winfo_height())
        except Exception:
            return False

    def _d_general_settings(self):
        win = ctk.CTkToplevel(self)
        win.title("General backlog workplaces")
        win.geometry(
            f"340x420+{self.winfo_rootx() + 220}+{self.winfo_rooty() + 150}"
        )
        win.configure(fg_color=LIGHT_PANEL)
        ctk.CTkLabel(win, text="Treat as general backlog",
                     text_color=LIGHT_TEXT, font=self.font_l
                     ).pack(anchor="w", padx=16, pady=(14, 2))
        ctk.CTkLabel(win, text="Cards from these workplaces live in the rail.",
                     text_color=LIGHT_MUTED, font=self.font_s
                     ).pack(anchor="w", padx=16, pady=(0, 8))
        present = sorted({t["team"] for t in self.planner_tasks})
        variables = {}
        scroller = ctk.CTkScrollableFrame(win, fg_color="transparent")
        scroller.pack(fill="both", expand=True, padx=10)
        for team in present:
            var = tk.BooleanVar(value=team in self._d_general)
            variables[team] = var
            ctk.CTkCheckBox(
                scroller, text=team, variable=var, onvalue=True,
                offvalue=False, font=self.font_s, text_color=LIGHT_TEXT,
                fg_color="#2f6fd0", hover_color="#255cb0",
            ).pack(anchor="w", padx=8, pady=4)

        def apply():
            self._d_general = {team for team, var in variables.items()
                               if var.get()}
            self._d_render_rail()
            self._d_redraw()
            win.destroy()

        buttons = ctk.CTkFrame(win, fg_color="transparent")
        buttons.pack(fill="x", padx=12, pady=10)
        ctk.CTkButton(buttons, text="Cancel", width=90, height=30,
                      fg_color="#eef1f6", text_color=LIGHT_TEXT,
                      hover_color="#dde2ea", command=win.destroy
                      ).pack(side="right", padx=4)
        ctk.CTkButton(buttons, text="Apply", width=90, height=30,
                      fg_color="#2f6fd0", hover_color="#255cb0",
                      command=apply).pack(side="right", padx=4)
        win.after(50, win.lift)

    def destroy(self):
        for job in (self._d_redraw_job, getattr(self, "_d_initial_redraw", None)):
            if job is not None:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
        self._d_redraw_job = None
        self._d_initial_redraw = None
        super().destroy()

    def _d_set_team(self, value):
        self._d_team_filter = value
        self._d_redraw()

    def _d_set_priority(self, value):
        self._d_priority_filter = value
        self._d_redraw()

    def _d_set_mode(self, mode):
        self._d_mode = mode
        self._d_update_mode_buttons()
        self._d_redraw()

    def _d_shift_day(self, step):
        self._d_mode = "day"
        self._d_day = max(0, min(4, self._d_day + step))
        self._d_update_mode_buttons()
        self._d_redraw()

    def _d_update_mode_buttons(self):
        buttons = getattr(self, "_d_mode_btns", {})
        for value, button in buttons.items():
            try:
                button.configure(
                    fg_color=LIGHT_TEXT if self._d_mode == value else "#eef1f6",
                    text_color="#ffffff" if self._d_mode == value
                    else LIGHT_MUTED,
                )
            except Exception:
                pass
        label = getattr(self, "_d_day_label", None)
        if label is not None:
            try:
                if self._d_mode == "day":
                    name, date = PLANNER_DAY_LABELS[self._d_day]
                    label.configure(text=f"{name} {date}")
                else:
                    label.configure(text="week")
            except Exception:
                pass

    def dirty_tasks(self):
        """Cards whose board values differ from the values loaded from SAP."""
        return [task for task in self.planner_tasks if task.get("dirty")]

    def _d_send(self):
        if self._on_send is not None:
            self._on_send()

    def _d_reload(self):
        if self._on_reload is not None:
            self._on_reload()

    def _d_update_send(self):
        dirty = sum(1 for task in self.planner_tasks if task.get("dirty"))
        conflicts = sum(1 for task in self.planner_tasks
                        if task.get("sap_conflict"))
        if self._d_send_btn is not None:
            try:
                self._d_send_btn.configure(
                    text=(f"Send changes ({dirty})" if dirty
                          else "Send changes"))
            except Exception:
                pass
        subtitle = getattr(self, "_d_subtitle", None)
        if subtitle is not None:
            try:
                subtitle.configure(
                    text=(f"{len(self.planner_tasks)} orders \u00b7 "
                          f"{len(self._d_teams())} teams \u00b7 {dirty} staged "
                          f"\u00b7 {conflicts} SAP changed"))
            except Exception:
                pass
    def _d_visible(self):
        out = []
        for task in self.planner_tasks:
            if task["team"] in self._d_general:
                continue
            if (self._d_team_filter != "All teams"
                    and task["team"] != self._d_team_filter):
                continue
            if (self._d_priority_filter != "All priorities"
                    and task["priority"] != self._d_priority_filter):
                continue
            out.append(task)
        return out

    def _d_schedule_redraw(self):
        if self._d_redraw_job is not None:
            try:
                self.after_cancel(self._d_redraw_job)
            except Exception:
                pass
        self._d_redraw_job = self.after(60, self._d_redraw_queued)

    def _d_redraw_queued(self):
        self._d_redraw_job = None
        self._d_redraw()

    def _d_geom(self):
        width = self._d_canvas.winfo_width() if self._d_canvas else 900
        height = self._d_canvas.winfo_height() if self._d_canvas else 600
        days = self._d_days()
        area = max(0, width - self.D_GUTTER - self._d_backlog_w() - 8)
        minimum = 220.0 if len(days) == 1 else 148.0
        return width, height, max(minimum, area / len(days))

    def _d_cap(self):
        return 8 if self._d_mode == "day" else self.D_CAP

    def _d_lane_height(self, items):
        if self._d_mode == "day":
            rows = max(1, min(self._d_cap(), len(items)))
            return 32 + rows * 48
        return self.D_LANE

    def _d_lanes(self):
        spans = []
        y = self.D_HEADER - self._d_offset
        total = 0
        for team in self._d_teams():
            items = [t for t in self._d_visible() if t["team"] == team]
            h = (self.D_LANE_COLLAPSED if team in self._d_collapsed
                 else self._d_lane_height(items))
            spans.append((team, y, h))
            y += h
            total += h
        return spans, total

    def _d_layout(self, tasks):
        """Rows per lane; a span reserves its days; overflow is counted."""
        cap = self._d_cap()
        rows = {}
        placed = []
        hidden = {}
        for task in sorted(tasks, key=lambda t: (t["start"],
                                                 t.get("rank", 0),
                                                 t["order"])):
            days = (["backlog"] if task["start"] < 0 else
                    list(range(task["start"],
                               min(5, task["start"] + task["span"]))))
            row = None
            for candidate in range(cap):
                if all(candidate not in rows.get(day, set()) for day in days):
                    row = candidate
                    break
            if row is None:
                bucket = "backlog" if task["start"] < 0 else task["start"]
                hidden.setdefault(bucket, []).append(task)
                continue
            for day in days:
                rows.setdefault(day, set()).add(row)
            placed.append((task, row))
        return placed, hidden

    def _d_hit(self, item):
        self._d_hits.append(item)

    def _d_round(self, x1, y1, x2, y2, radius, **kw):
        points = [
            x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
            x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        ]
        return self._d_canvas.create_polygon(points, smooth=True, **kw)

    def _d_truncate(self, text, width):
        limit = max(8, int(width / 5.4))
        return text if len(text) <= limit else text[:limit - 1].rstrip() + "\u2026"

    def _d_truncate_floc(self, text, width):
        limit = max(4, int(width / 3.6))
        return text if len(text) <= limit else text[:limit - 1].rstrip() + "\u2026"

    def _d_fit(self, text, font, width):
        if width <= 4:
            return ""
        if font.measure(text) <= width:
            return text
        text = text.rstrip()
        while text and font.measure(text + "\u2026") > width:
            text = text[:-1]
        return text.rstrip() + "\u2026"

    def _d_lane_panel(self, team, y, h, items):
        canvas = self._d_canvas
        top = max(y, self.D_HEADER)
        general = team in self._d_general
        canvas.create_rectangle(0, top, self.D_GUTTER, y + h,
                                fill=LIGHT_PANEL, outline="")
        canvas.create_rectangle(self.D_GUTTER, top,
                                self._d_days_x(), y + h,
                                fill="#fffaf0" if general else "#fbfcfe",
                                outline="#f0d9a8" if general else "")
        self._d_hit((0, y, self.D_GUTTER, y + h, "lane", team))
        self._d_hit((self.D_GUTTER, y, self._d_days_x(),
                     y + h, "backlog", team))
        collapsed = team in self._d_collapsed
        chevron = "\u25b8" if collapsed else "\u25be"
        canvas.create_text(12, y + 14, text=chevron, anchor="nw",
                           fill=LIGHT_MUTED, font=("Segoe UI", 10, "bold"))
        title = f"{team} \u00b7 backlog" if general else team
        canvas.create_text(28, y + 11, text=title, anchor="nw",
                           fill="#9a6212" if general else LIGHT_TEXT,
                           font=("Segoe UI", 10, "bold"))
        hours = sum((t["span"] if t["start"] >= 0 else 1) * 4 for t in items)
        canvas.create_text(28, y + 29,
                           text=f"{len(items)} orders \u00b7 {hours}h",
                           anchor="nw", fill=LIGHT_MUTED,
                           font=("Segoe UI", 8))
        if collapsed:
            self._d_collapsed_strip(items, y, h)
            if self._d_backlog_folded:
                backlog = [t for t in items if t["start"] < 0]
                canvas.create_text(
                    self.D_GUTTER + self._d_backlog_w() / 2, y + 20,
                    text=str(len(backlog)), anchor="center",
                    fill="#b07a1f" if backlog else LIGHT_MUTED,
                    font=("Segoe UI", 10, "bold"))
            return
        bar_w = self.D_GUTTER - 44
        frac = min(1.0, hours / 120.0)
        canvas.create_rectangle(28, y + 43, 28 + bar_w, y + 47,
                                fill="#eef1f6", outline="")
        canvas.create_rectangle(28, y + 43, 28 + bar_w * frac, y + 47,
                                fill="#f4b860" if hours > 90 else "#54c6a3",
                                outline="")
        if self._d_backlog_folded:
            backlog = [t for t in items if t["start"] < 0]
            canvas.create_text(
                self.D_GUTTER + self._d_backlog_w() / 2, y + 24,
                text=str(len(backlog)), anchor="center",
                fill="#b07a1f" if backlog else LIGHT_MUTED,
                font=("Segoe UI", 11, "bold"))
            canvas.create_text(
                self.D_GUTTER + self._d_backlog_w() / 2, y + 40,
                text="\u25b8", anchor="center", fill=LIGHT_MUTED,
                font=("Segoe UI", 9))

    def _d_collapsed_strip(self, items, y, h):
        """Use a collapsed lane's row to show what is hidden."""
        canvas = self._d_canvas
        _width, _height, day_w = self._d_geom()
        days = self._d_days()
        x_start = self._d_days_x() - self._d_xoffset
        backlog = [t for t in items if t["start"] < 0]
        if not self._d_backlog_folded:
            canvas.create_text(self.D_GUTTER + 10, y + 18,
                               text=f"{len(backlog)} unplanned",
                               anchor="nw",
                               fill="#b07a1f" if backlog else LIGHT_MUTED,
                               font=("Segoe UI", 8, "bold"))
        for index, day in enumerate(days):
            x = x_start + index * day_w
            covering = [t for t in items
                        if t["start"] >= 0 and t["start"] <= day
                        < min(5, t["start"] + t["span"])]
            if not covering:
                continue
            canvas.create_text(x + 10, y + 10, text=f"{len(covering)}",
                               anchor="nw", fill=LIGHT_TEXT,
                               font=("Segoe UI", 10, "bold"))
            for offset, task in enumerate(sorted(
                    covering, key=lambda t: t["priority"])[:3]):
                colour = PRIORITY_BY_CODE.get(task["priority"], "#8ab4f8")
                cx = x + 12 + offset * 12
                canvas.create_oval(cx - 3, y + 29, cx + 3, y + 35,
                                   fill=colour, outline="")
        high = sum(1 for task in items if task["priority"] == "1")
        canvas.create_text(28, y + 38,
                           text=f"{high} high priority", anchor="nw",
                           fill="#b07a1f" if high else "#b3bccb",
                           font=("Segoe UI", 8))

    def _d_redraw(self):
        canvas = self._d_canvas
        if canvas is None:
            return
        if self._d_redraw_job is not None:
            try:
                self.after_cancel(self._d_redraw_job)
            except Exception:
                pass
            self._d_redraw_job = None
        try:
            if not canvas.winfo_exists():
                return
        except Exception:
            return
        width, height, day_w = self._d_geom()
        days = self._d_days()
        content_w = self._d_days_x() + day_w * len(days)
        self._d_maxxoff = max(0, int(content_w - width))
        if self._d_xoffset > self._d_maxxoff:
            self._d_xoffset = self._d_maxxoff
        if self._d_xoffset < 0:
            self._d_xoffset = 0
        x_start = self._d_days_x() - self._d_xoffset
        first_day, last_day = days[0], days[-1]

        visible = self._d_visible()
        by_team = {team: [t for t in visible if t["team"] == team]
                   for team in self._d_teams()}

        _spans, content_h = self._d_lanes()
        view = max(1, height - self.D_HEADER)
        self._d_maxoff = max(0, content_h - view)
        if self._d_offset > self._d_maxoff:
            self._d_offset = self._d_maxoff
        if self._d_offset < 0:
            self._d_offset = 0

        spans, content_h = self._d_lanes()
        canvas.delete("all")
        self._d_hits = []
        self._d_preview = []

        for index, (team, y, h) in enumerate(spans):
            if y + h < self.D_HEADER or y > height:
                continue
            general = team in self._d_general
            fill = ("#fffdf7" if general else
                    ("#ffffff" if index % 2 == 0 else "#f7f9fc"))
            canvas.create_rectangle(self.D_GUTTER, max(y, self.D_HEADER),
                                    width, y + h, fill=fill, outline="")
            for day_index in range(len(days)):
                x = x_start + day_index * day_w
                if x + day_w < self._d_days_x() or x > width:
                    continue
                if day_index:
                    canvas.create_line(x, self.D_HEADER, x, height,
                                       fill="#eef1f6")

            items = by_team[team]
            placed, hidden = ([], {}) if team in self._d_collapsed \
                else self._d_layout(items)

            for task, row in placed:
                if task["start"] < 0:
                    continue
                end = min(4, task["start"] + task["span"] - 1)
                vis_start = max(task["start"], first_day)
                vis_end = min(end, last_day)
                if vis_end < vis_start:
                    continue
                x1 = x_start + (vis_start - first_day) * day_w + 4
                card_w = (vis_end - vis_start + 1) * day_w - 8
                if (x1 + card_w < self._d_days_x()
                        or x1 > width):
                    continue
                self._d_card(x1, y + 10 + row * 48, card_w, task)

            for bucket, hidden_tasks in hidden.items():
                if bucket == "backlog":
                    continue
                if bucket < first_day or bucket > last_day:
                    continue
                x1 = x_start + (bucket - first_day) * day_w + 4
                if (x1 + day_w < self._d_days_x()
                        or x1 > width):
                    continue
                self._d_more_chip(x1, y + 10 + self._d_cap() * 48 + 2,
                                  day_w - 8, team, bucket, hidden_tasks)

            # pinned gutter + backlog, over any day card bleed
            self._d_lane_panel(team, y, h, items)
            if team in self._d_collapsed or self._d_backlog_folded:
                continue

            for task, row in placed:
                if task["start"] >= 0:
                    continue
                self._d_card(self.D_GUTTER + 7, y + 10 + row * 48,
                             self._d_backlog_w() - 14, task)
            if "backlog" in hidden:
                self._d_more_chip(self.D_GUTTER + 7,
                                  y + 10 + self._d_cap() * 48 + 2,
                                  self._d_backlog_w() - 14, team, "backlog",
                                  hidden["backlog"])

        # pinned header row
        canvas.create_rectangle(0, 0, width, self.D_HEADER,
                                fill=LIGHT_PANEL, outline="")
        canvas.create_line(0, self.D_HEADER, width, self.D_HEADER,
                           fill=LIGHT_BORDER)
        for index, day in enumerate(days):
            x = x_start + index * day_w
            if x + day_w < self._d_days_x() or x > width:
                continue
            name, date = PLANNER_DAY_LABELS[day]
            count = sum(
                1 for t in visible
                if t["start"] >= 0 and t["start"] <= day
                < min(5, t["start"] + t["span"]))
            suffix = " \u00b7 single day" if len(days) == 1 else ""
            canvas.create_text(x + 10, 11, text=f"{name} {date}{suffix}",
                               anchor="nw", fill=LIGHT_TEXT,
                               font=("Segoe UI", 10, "bold"))
            canvas.create_text(x + 10, 30, text=f"{count} cards", anchor="nw",
                               fill=LIGHT_MUTED, font=("Segoe UI", 8))

        canvas.create_rectangle(0, 0, self._d_days_x(),
                                self.D_HEADER, fill=LIGHT_PANEL, outline="")
        canvas.create_text(12, 12, text="TEAM", anchor="nw",
                           fill=LIGHT_MUTED, font=("Segoe UI", 8, "bold"))
        canvas.create_text(12, 30, text="click to collapse", anchor="nw",
                           fill="#b3bccb", font=("Segoe UI", 8))
        backlog_count = sum(1 for t in visible if t["start"] < 0)
        if self._d_backlog_folded:
            canvas.create_text(
                self.D_GUTTER + self._d_backlog_w() / 2, 18,
                text="\u25b8", anchor="center", fill=LIGHT_MUTED,
                font=("Segoe UI", 9))
            canvas.create_text(
                self.D_GUTTER + self._d_backlog_w() / 2, 38,
                text=f"{backlog_count}", anchor="center",
                fill="#b07a1f" if backlog_count else LIGHT_MUTED,
                font=("Segoe UI", 10, "bold"))
        else:
            canvas.create_text(self.D_GUTTER + 10, 12, text="BACKLOG",
                               anchor="nw", fill=LIGHT_MUTED,
                               font=("Segoe UI", 8, "bold"))
            canvas.create_text(self.D_GUTTER + 10, 30,
                               text=f"{backlog_count} unplanned", anchor="nw",
                               fill="#b3bccb", font=("Segoe UI", 8))
        self._d_hit((self.D_GUTTER, 0, self._d_days_x(), self.D_HEADER,
                     "backlog-toggle", None))
        canvas.create_line(self.D_GUTTER, 0, self.D_GUTTER, height,
                           fill=LIGHT_BORDER)
        canvas.create_line(self._d_days_x(), 0,
                           self._d_days_x(), height,
                           fill=LIGHT_BORDER)

        if self._d_maxoff:
            first = self._d_offset / self._d_maxoff
            last = min(1.0, first + view / max(1, content_h))
        else:
            first, last = 0.0, 1.0
        try:
            self._d_sb.set(first, last)
        except Exception:
            pass
        if self._d_maxxoff:
            first_x = self._d_xoffset / self._d_maxxoff
            last_x = min(1.0, first_x + width / max(1.0, content_w))
        else:
            first_x, last_x = 0.0, 1.0
        try:
            self._d_hsb.set(first_x, last_x)
        except Exception:
            pass

    def _d_card(self, x1, y1, card_w, task):
        selected = task["id"] == self._d_selected
        dirty = bool(task.get("dirty"))
        conflict = bool(task.get("sap_conflict"))
        if conflict:
            outline, fill, thick = "#d13438", "#fff7f7", 2
        elif dirty:
            outline, fill, thick = "#d99b2b", "#fffdf5", 2
        elif selected:
            outline, fill, thick = "#2f6fd0", "#eef4ff", 2
        else:
            outline, fill, thick = "#dde3ec", "#ffffff", 1
        self._d_round(x1, y1, x1 + card_w, y1 + self.D_CARD_H, 7,
                      fill=fill, outline=outline, width=thick)
        self._d_canvas.create_rectangle(
            x1 + 1, y1 + 6, x1 + 5, y1 + self.D_CARD_H - 6,
            fill=PRIORITY_BY_CODE.get(task["priority"], "#f4b860"),
            outline="")
        self._d_canvas.create_text(
            x1 + 11, y1 + 5, text=task["order"], anchor="nw",
            fill=LIGHT_TEXT, font=("Segoe UI", 9, "bold"))
        order_w = self._d_font_bold.measure(task["order"]) + 8
        floc = self._d_floc(task)
        floc_room = card_w - order_w - 8
        if floc and floc_room > 18:
            self._d_canvas.create_text(
                x1 + 11 + order_w, y1 + 6,
                text=self._d_fit(floc, self._d_font_small, floc_room),
                anchor="nw", fill="#8a94a6", font=("Segoe UI", 6))
        self._d_canvas.create_text(
            x1 + 11, y1 + 21,
            text=self._d_fit(task["title"], self._d_font_title, card_w - 18),
            anchor="nw", fill="#4a5363", font=("Segoe UI", 8))
        if conflict or dirty:
            state = "#d13438" if conflict else "#d99b2b"
            self._d_canvas.create_rectangle(
                x1 + card_w - 4, y1 + 5, x1 + card_w - 1,
                y1 + self.D_CARD_H - 5, fill=state, outline="")
        if selected:
            self._d_canvas.create_rectangle(
                x1 + 1, y1 + 8, x1 + 3, y1 + self.D_CARD_H - 8,
                fill="#2f6fd0", outline="")
            self._d_canvas.create_rectangle(
                x1 + card_w - 3, y1 + 8, x1 + card_w - 1,
                y1 + self.D_CARD_H - 8, fill="#2f6fd0", outline="")
        self._d_hit((x1, y1, x1 + card_w, y1 + self.D_CARD_H,
                     "card", task["id"]))
        if task["start"] >= 0:
            self._d_hit((x1, y1, x1 + 7, y1 + self.D_CARD_H,
                         "resize-l", task["id"]))
            self._d_hit((x1 + card_w - 7, y1, x1 + card_w,
                         y1 + self.D_CARD_H, "resize-r", task["id"]))

    def _d_more_chip(self, x1, y1, card_w, team, bucket, hidden):
        self._d_round(x1, y1, x1 + card_w, y1 + 18, 6, fill="#e9edf4",
                      outline="#dde3ec")
        self._d_canvas.create_text(
            x1 + 8, y1 + 9, text=f"+{len(hidden)} more", anchor="w",
            fill="#5b6577", font=("Segoe UI", 8, "bold"))
        self._d_hit((x1, y1, x1 + card_w, y1 + 18, "more",
                     (team, bucket, hidden)))

    def _d_at(self, x, y):
        for (x1, y1, x2, y2, kind, data) in reversed(self._d_hits):
            if x1 <= x <= x2 and y1 <= y <= y2:
                return kind, data
        return None, None

    def _d_day_at_x(self, x):
        days = self._d_days()
        _width, _height, day_w = self._d_geom()
        content_x = x - (self._d_days_x()) + self._d_xoffset
        index = int(content_x // day_w)
        if index < 0 or index >= len(days):
            return None
        return days[index]

    def _d_lane_at_y(self, y):
        for (team, lane_y, lane_h) in self._d_lanes()[0]:
            if lane_y <= y <= lane_y + lane_h:
                return team, lane_y, lane_h
        return None, None, None

    def _d_bucket(self, team, start):
        return sorted([t for t in self.planner_tasks
                       if t["team"] == team and t["start"] == start],
                      key=lambda t: (t.get("rank", 0), t["order"]))

    def _d_insert_info(self, team, start, lane_y, y):
        """Index and line Y in the same visual order the cards are drawn."""
        placed, _hidden = self._d_layout(
            [t for t in self._d_visible() if t["team"] == team])
        bucket = [(task, row) for task, row in placed
                  if task["start"] == start]
        top = lane_y + 10
        for index, (_task, row) in enumerate(bucket):
            centre = top + row * 48 + 24
            if y < centre:
                return index, top + row * 48 - 4
        if bucket:
            last_row = bucket[-1][1]
            return len(bucket), top + last_row * 48 + 48 - 4
        return 0, top - 4

    def _d_drop_target(self, x, y):
        if y < self.D_HEADER or x < self.D_GUTTER:
            return None
        team, lane_y, _lane_h = self._d_lane_at_y(y)
        if team is None:
            return None
        if x < self._d_days_x():
            start = -1
        else:
            day = self._d_day_at_x(x)
            if day is None:
                return None
            start = day
        index, _line_y = self._d_insert_info(team, start, lane_y, y)
        return team, start, index

    def _d_clear_preview(self):
        for item in self._d_preview:
            self._d_canvas.delete(item)
        self._d_preview = []

    def _d_draw_drop_preview(self, x, y, task):
        self._d_clear_preview()
        target = self._d_drop_target(x, y)
        if target is None:
            return
        team, start, index = target
        team_y = None
        for (name, lane_y, lane_h) in self._d_lanes()[0]:
            if name == team:
                team_y, team_h = lane_y, lane_h
                break
        if team_y is None:
            return
        if start < 0:
            x1 = self.D_GUTTER + 4
            x2 = self._d_days_x() - 4
        else:
            days = self._d_days()
            if start not in days:
                return
            x1 = (self._d_days_x() - self._d_xoffset
                  + days.index(start) * self._d_geom()[2] + 4)
            x2 = x1 + self._d_geom()[2] - 8
        self._d_preview.append(self._d_canvas.create_rectangle(
            x1, team_y + 2, x2, team_y + team_h - 2,
            outline="#2f6fd0", width=2, dash=(4, 3)))
        _index, line_y = self._d_insert_info(team, start, team_y, y)
        self._d_preview.append(self._d_canvas.create_line(
            x1, line_y, x2, line_y, fill="#2f6fd0", width=3))

    def _d_draw_resize_preview(self, task, start, span):
        self._d_clear_preview()
        days = self._d_days()
        team = task["team"]
        team_y = None
        for (name, lane_y, _lane_h) in self._d_lanes()[0]:
            if name == team:
                team_y = lane_y
                break
        if team_y is None:
            return
        placed, _hidden = self._d_layout(
            [t for t in self.planner_tasks if t["team"] == team])
        row = next((r for t, r in placed if t is task), 0)
        end = min(4, start + span - 1)
        vis_start, vis_end = max(start, days[0]), min(end, days[-1])
        if vis_end < vis_start:
            return
        day_w = self._d_geom()[2]
        x1 = (self._d_days_x() - self._d_xoffset
              + (vis_start - days[0]) * day_w + 4)
        card_w = (vis_end - vis_start + 1) * day_w - 8
        self._d_preview.append(self._d_round(
            x1, team_y + 10 + row * 48, x1 + card_w,
            team_y + 10 + row * 48 + self.D_CARD_H, 7,
            fill="", outline="#2f6fd0", width=2))

    def _d_press(self, event):
        kind, data = self._d_at(event.x, event.y)
        if kind in ("card", "resize-l", "resize-r"):
            task = self._d_task(data)
            if task is None:
                return
            self._d_selected = data
            self._d_drag = {
                "kind": "resize" if kind.startswith("resize") else "card",
                "edge": kind.replace("resize-", ""),
                "id": data, "x": event.x, "y": event.y,
                "moved": False,
                "origin_start": task["start"],
                "origin_span": task["span"],
            }
            self._d_render_detail()
            self._d_redraw()
        elif kind == "lane":
            if data in self._d_collapsed:
                self._d_collapsed.remove(data)
            else:
                self._d_collapsed.add(data)
            self._d_redraw()
        elif kind == "backlog-toggle":
            self._d_backlog_folded = not self._d_backlog_folded
            self._d_redraw()
        elif kind == "more":
            team, bucket, hidden = data
            self._d_more_popover(team, bucket, hidden)

    def _d_hover(self, event):
        if self._d_drag:
            return
        kind, _data = self._d_at(event.x, event.y)
        cursor = {
            "resize-l": "sb_h_double_arrow",
            "resize-r": "sb_h_double_arrow",
            "card": "fleur",
            "lane": "hand2",
            "more": "hand2",
        }.get(kind, "")
        self._d_canvas.configure(cursor=cursor)

    def _d_motion(self, event):
        drag = self._d_drag
        if not drag:
            return
        if abs(event.x - drag["x"]) + abs(event.y - drag["y"]) > 6:
            drag["moved"] = True
        task = self._d_task(drag["id"])
        if task is None:
            return
        if drag["kind"] == "resize":
            day = self._d_day_at_x(event.x)
            if day is None:
                return
            if drag["edge"] == "r":
                end = max(drag["origin_start"], day)
                start = drag["origin_start"]
                span = end - start + 1
            else:
                end = min(4, drag["origin_start"] + drag["origin_span"] - 1)
                start = min(day, end)
                span = end - start + 1
            self._d_draw_resize_preview(task, start, span)
            return
        self._d_clear_preview()
        self._d_clear_board_ghost()
        if self._d_over_rail(event.x_root, event.y_root):
            self._d_rail_draw_overlay(event.x_root, event.y_root, task)
            return
        self._d_rail_clear_overlay()
        self._d_board_ghost(event.x, event.y, task)
        self._d_draw_drop_preview(event.x, event.y, task)

    def _d_release(self, event):
        drag = self._d_drag
        if not drag:
            return
        self._d_clear_preview()
        self._d_rail_clear_overlay()
        self._d_clear_board_ghost()
        self._d_drag = None
        task = self._d_task(drag["id"])
        if task is None:
            return
        if drag["kind"] == "resize":
            if not drag["moved"]:
                self._d_render_detail()
                return
            day = self._d_day_at_x(event.x)
            if day is None:
                self._d_redraw()
                return
            if drag["edge"] == "r":
                start = drag["origin_start"]
                end = max(start, day)
            else:
                end = min(4, drag["origin_start"] + drag["origin_span"] - 1)
                start = min(day, end)
            task["start"] = start
            task["span"] = end - start + 1
            task["dirty"] = True
            self._d_render_detail()
            self._d_update_send()
            self._d_render_rail()
            self._d_redraw()
            return
        if not drag["moved"]:
            self._d_render_detail()
            return
        if self._d_over_rail(event.x_root, event.y_root):
            self._d_rail_insert(task,
                                self._d_rail_index_from_root(event.y_root))
            return
        target = self._d_drop_target(event.x, event.y)
        if target is None:
            self._d_redraw()
            return
        self._d_apply_drop(task, *target)

    def _d_apply_drop(self, task, team, start, index):
        self._d_reorder(task, team, start, index)
        task["team"] = team
        if start < 0:
            task["start"] = -1
            task["span"] = 1
        else:
            task["start"] = start
            task["span"] = max(1, min(task["span"], 5 - start))
        task["dirty"] = True
        self._d_render_detail()
        self._d_update_send()
        self._d_render_rail()
        self._d_redraw()

    def _d_reorder(self, task, team, start, index):
        bucket = self._d_bucket(team, start)
        current = next((i for i, item in enumerate(bucket) if item is task),
                       None)
        if current is not None and current < index:
            index -= 1
        bucket = [item for item in bucket if item is not task]
        index = max(0, min(index, len(bucket)))
        bucket.insert(index, task)
        for rank, item in enumerate(bucket):
            item["rank"] = rank

    def _d_target(self, x, y):
        target = self._d_drop_target(x, y)
        if target is None:
            return None
        team, start, _index = target
        return team, (None if start < 0 else start)
    def _d_wheel(self, event):
        self._d_offset -= int(event.delta / 120) * 48
        self._d_redraw()

    def _d_scroll(self, *args):
        if not args:
            return
        if args[0] == "moveto":
            self._d_offset = int(float(args[1]) * self._d_maxoff)
        elif args[0] == "scroll":
            amount = int(args[1])
            if len(args) > 2 and args[2] == "pages":
                step = max(48, int(
                    (self._d_canvas.winfo_height() - self.D_HEADER) * 0.9))
            else:
                step = 48
            self._d_offset += amount * step
        self._d_redraw()

    def _d_scroll_x(self, *args):
        if not args:
            return
        if args[0] == "moveto":
            self._d_xoffset = int(float(args[1]) * self._d_maxxoff)
        elif args[0] == "scroll":
            amount = int(args[1])
            step = 80 if (len(args) < 3 or args[2] != "pages") else 400
            self._d_xoffset += amount * step
        self._d_redraw()

    def _d_more_popover(self, team, bucket, hidden):
        win = ctk.CTkToplevel(self)
        win.title(f"{len(hidden)} hidden in {team}")
        win.geometry(
            f"380x340+{self.winfo_rootx() + 240}+{self.winfo_rooty() + 170}"
        )
        win.configure(fg_color=LIGHT_PANEL)
        where = ("Backlog" if bucket == "backlog" else
                 f"{PLANNER_DAY_LABELS[bucket][0]} "
                 f"{PLANNER_DAY_LABELS[bucket][1]}")
        ctk.CTkLabel(win, text=f"{team} \u00b7 {where}",
                     text_color=LIGHT_TEXT, font=self.font_l
                     ).pack(anchor="w", padx=16, pady=(14, 2))
        ctk.CTkLabel(win, text=f"{len(hidden)} cards hidden by the 3-card cap",
                     text_color=LIGHT_MUTED, font=self.font_s
                     ).pack(anchor="w", padx=16, pady=(0, 8))
        scroller = ctk.CTkScrollableFrame(win, fg_color="transparent")
        scroller.pack(fill="both", expand=True, padx=10, pady=(0, 12))
        for task in hidden:
            row = ctk.CTkFrame(scroller, fg_color="#f5f7fb", corner_radius=7)
            row.pack(fill="x", pady=3)
            ctk.CTkLabel(row, text=task["order"], text_color=LIGHT_TEXT,
                         font=self.font_s).pack(side="left", padx=10, pady=6)
            ctk.CTkLabel(row, text=self._d_truncate(task["title"], 150),
                         text_color=LIGHT_MUTED, font=self.font_s
                         ).pack(side="left", padx=(0, 8))
        win.after(50, win.lift)
