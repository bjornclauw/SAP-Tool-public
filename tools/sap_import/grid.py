"""ImportGrid — the SAP Import grid, rendered by tksheet.

The window owns the queue; this module owns the canvas grid and the mapping
between a display row and the queue row it stands for. Everything the window
needs about rendering a row lives here, so the window never touches tksheet
directly. Rows keep the queue's order — the grid never sorts or reorders.

Interactions reported back to the window:
    on_edit(row)                    double click on a non-dropdown cell
    on_dropdown(row, col, value)    a constrained cell's dropdown picked a value
    on_menu(row, event)             right click on a row
    on_column_resized(col, width)   the user dragged a column border
    on_select()                     the row selection changed
"""

from __future__ import annotations

import tkinter.font as tkfont
from collections.abc import Callable

import tksheet
from ui import ui_styles

from .failures import has_failure
from .models import COL_WIDTHS, STATUS_LABELS, auto_upload_eligible
from .palette import cell_colour, cell_text_colour

_MIN_WIDTH, _MAX_WIDTH, _WIDTH_SAMPLE = 40, 380, 200

# Whole-row selection drives bulk edits (tksheet's Binding type omits these).
# "single_select" sets the anchor a plain click makes, "drag_select" is what
# enables Shift+click/-drag range selection, and "ctrl_click_select" the
# Ctrl+click toggle; the *_row_select names alone don't flip those flags.
_SELECT_BINDINGS = ("single_select", "drag_select", "ctrl_click_select",
                    "row_select", "select_all", "deselect")


class ImportGrid:
    def __init__(
        self,
        parent,
        on_edit: Callable[[dict], None],
        on_menu: Callable[[dict, object], None],
        on_dropdown: Callable[[dict, str, str], None],
        on_column_resized: Callable[[str, int], None] | None = None,
        on_select: Callable[[], None] | None = None,
    ):
        self._on_edit = on_edit
        self._on_menu = on_menu
        self._on_dropdown = on_dropdown
        self._on_column_resized = on_column_resized
        self._on_select = on_select

        self._columns: list[str] = []
        self._display_names: dict[str, str] = {}
        self._saved_widths: dict[str, int] = {}
        self._rows: list[dict] = []
        self._index: dict[int, int] = {}
        self._dropdowns: dict[str, list[str]] = {}
        self._wo_keys_by_type: dict[str, list[str]] = {}

        self.sheet = tksheet.Sheet(parent, headers=[], data=[])
        # Display only apart from the constrained-cell dropdowns: every other
        # edit goes through the modal, so a stray keystroke can never diverge
        # the grid from the queue. Column reorder is owned by the Columns menu,
        # so tksheet's drag-reorder stays off. tksheet handles "edit_bindings"
        # at runtime but its Binding type omits it.
        self.sheet.disable_bindings("edit_bindings")  # type: ignore[arg-type]
        # Whole-row selection (index gutter, drag, Shift/Ctrl, Ctrl+A) drives
        # bulk edits; the in-cell dropdowns stay on cell clicks.
        self.sheet.enable_bindings(*_SELECT_BINDINGS)  # type: ignore[arg-type]
        if on_column_resized is not None:
            self.sheet.enable_bindings("column_width_resize")
        self.sheet.pack(fill="both", expand=True)
        self._bind()

    # ---- bindings ---------------------------------------------------------
    def _bind(self):
        self.sheet.bind("<ButtonPress-1>", self._on_click)
        self.sheet.bind("<Double-Button-1>", self._on_double_click)
        self.sheet.bind(self.sheet.ops.rc_bindings[0], self._on_right_click)
        if self._on_column_resized is not None:
            self.sheet.extra_bindings("column_width_resize", self._resized)
        on_select = self._on_select
        if on_select is not None:
            self.sheet.bind("<<SheetSelect>>", lambda _data, cb=on_select: cb())

    def _resized(self, event):
        if self._on_column_resized is None:
            return
        for col, sizes in (event.get("resized_columns") or {}).items():
            if 0 <= col < len(self._columns):
                self._on_column_resized(self._columns[col], int(sizes["new_size"]))

    # ---- columns ----------------------------------------------------------
    @property
    def columns(self) -> list[str]:
        return list(self._columns)

    def set_columns(
        self,
        columns: list[str],
        display_names: dict[str, str],
        saved_widths: dict[str, int] | None = None,
    ):
        self._columns = list(columns)
        self._display_names = dict(display_names)
        self._saved_widths = dict(saved_widths or {})
        # Dropdowns are keyed by column index, so a reorder would otherwise
        # leave a stale dropdown on whatever column lands on an old index.
        self.sheet.reset_all_options()
        self.set_headers([self._display_names.get(c, c) for c in self._columns])
        self._apply_dropdowns()
        # The visible column set may have changed, so rebuild the column
        # positions for the new count; otherwise columns past the old count
        # never get laid out and stay hidden.
        self._render(reset_columns=True)
        self._apply_widths()

    def set_headers(self, labels: list[str]):
        self.sheet.headers(list(labels))

    def _apply_widths(self):
        """Saved width where the user set one, else a content-based auto width."""
        font = tkfont.Font(font=ui_styles.FONTS["body"])
        for i, name in enumerate(self._columns):
            width = self._saved_widths.get(name) or self._auto_width(name, font)
            self.sheet.column_width(i, width)

    def _auto_width(self, name: str, font: tkfont.Font) -> int:
        width = max(font.measure(self._display_names.get(name, name)),
                    COL_WIDTHS.get(name, _MIN_WIDTH))
        for row in self._rows[:_WIDTH_SAMPLE]:
            text = self._display_value(name, row)
            if text:
                width = max(width, font.measure(text))
        return min(max(width + 18, _MIN_WIDTH), _MAX_WIDTH)

    # ---- dropdowns --------------------------------------------------------
    def set_dropdowns(self, columns: dict[str, list[str]]):
        """Offer an in-cell picker on each constrained column.

        Read-only pickers (no free typing); a pick is reported to the window
        through ``on_dropdown``. Callers refresh the lists when they change.
        """
        self._dropdowns = {name: list(values) for name, values in columns.items()}
        self._apply_dropdowns()
        self._apply_wo_key_dropdowns()

    def set_wo_key_map(self, mapping: dict[str, list[str]]):
        """The WO_KEYs each WO_TYPE owns (drives the per-row WO_KEY picker)."""
        self._wo_keys_by_type = {k: list(v) for k, v in mapping.items()}
        self._apply_wo_key_dropdowns()

    def _apply_dropdowns(self):
        for i, name in enumerate(self._columns):
            values = self._dropdowns.get(name)
            if not values:
                continue
            self.sheet.dropdown_column(
                i, values=list(values), state="readonly", edit_data=False,
                selection_function=lambda event, n=name: self._select(n, event),
                redraw=False,
            )

    def _apply_wo_key_dropdowns(self, indices: list[int] | None = None):
        """Narrow WO_KEY per row to the keys its WO_TYPE owns."""
        col = self._columns.index("WO_KEY") if "WO_KEY" in self._columns else None
        all_keys = self._dropdowns.get("WO_KEY")
        if col is None or not all_keys:
            return
        for r in range(len(self._rows)) if indices is None else indices:
            allowed = self._wo_keys_by_type.get(
                str(self._rows[r].get("WO_TYPE", "")).strip()) or all_keys
            self.sheet.dropdown_cell(
                r, col, values=list(allowed), state="readonly", edit_data=False,
                selection_function=lambda event: self._select("WO_KEY", event),
                redraw=False,
            )

    def dropdown_values(self, column: str) -> list[str]:
        return list(self._dropdowns.get(column, []))

    def _is_dropdown_cell(self, col: int | None) -> bool:
        return (col is not None and 0 <= col < len(self._columns)
                and self._columns[col] in self._dropdowns)

    def _select(self, name: str, event):
        row = self._row_at(event.row)
        if row is not None:
            self._on_dropdown(row, name, str(event.value))

    # ---- rows -------------------------------------------------------------
    def refresh(self, rows: list[dict]):
        """Replace the whole displayed set (used on fetch/filter)."""
        self._rows = list(rows)
        self._index = {id(r): i for i, r in enumerate(self._rows)}
        self._render()

    def update_row(self, row: dict) -> bool:
        """Update one displayed row in place. False when the row isn't shown."""
        idx = self._index.get(id(row))
        if idx is None:
            return False
        self._rows[idx] = row
        self.sheet.set_row_data(idx, self._values(row), redraw=False)
        self._apply_wo_key_dropdowns([idx])
        self._paint_row(idx, row)
        return True

    def _render(self, reset_columns: bool = False):
        # On a plain refresh keep the widths set in set_columns/auto-fit; when
        # the column set itself changed, reset positions so every column (new
        # ones included) gets laid out.
        self.sheet.set_sheet_data([self._values(r) for r in self._rows],
                                  reset_col_positions=reset_columns, redraw=False)
        self.sheet.dehighlight_rows("all", redraw=False)
        self.sheet.dehighlight_cells(row="all", canvas="table", redraw=False)
        self._apply_wo_key_dropdowns()
        for i, row in enumerate(self._rows):
            self._paint_row(i, row, redraw=False)
        self.sheet.set_refresh_timer(True)

    def _paint_row(self, idx: int, row: dict, redraw: bool = True):
        """Repaint one row: the Excel per-cell palette, or red on a failure.

        A failed row is red end-to-end, so the failure signal wins over the
        palette; its per-cell colours are cleared first.
        """
        self._clear_cell_colours(idx)
        if has_failure(row):
            self.sheet.highlight_rows(idx, bg=ui_styles.COLORS["error_light"],
                                      fg=ui_styles.COLORS["error"],
                                      highlight_index=False, redraw=False)
            self.sheet.set_refresh_timer(redraw)
            return
        self.sheet.dehighlight_rows([idx], redraw=False)
        for col, name in enumerate(self._columns):
            bg = cell_colour(row, name)
            fg = cell_text_colour(row, name)
            if bg or fg:
                self.sheet.highlight_cells(cells=[(idx, col)], bg=bg or False,
                                           fg=fg or False, redraw=False)
        self.sheet.set_refresh_timer(redraw)

    def _clear_cell_colours(self, idx: int):
        cells = [(idx, c) for c in range(len(self._columns))]
        if cells:
            self.sheet.dehighlight_cells(cells=cells, redraw=False)

    def _values(self, row: dict) -> list[str]:
        return [self._display_value(c, row) for c in self._columns]

    def _display_value(self, name: str, row: dict) -> str:
        if name == "Status":
            code = row.get("Status", "0")
            return f"{code} · {STATUS_LABELS.get(code, code)}"
        if name == "Auto":
            # Filled dot = the next auto-upload tick would send this row.
            return "\u25cf" if auto_upload_eligible(row) else "\u25cb"
        if name == "REV":
            return self._rev_text(row)
        return str(row.get(name, ""))[:80]

    def _rev_text(self, row: dict) -> str:
        rev = str(row.get("REV", ""))
        original = str(row.get("_original_rev", ""))
        if original and original != rev:
            return f"{rev} ← {original}"[:80]
        return rev[:80]

    def _row_at(self, displayed_row: int) -> dict | None:
        if 0 <= displayed_row < len(self._rows):
            return self._rows[displayed_row]
        return None

    def selected_rows(self) -> list[dict]:
        """The queue rows behind the current selection, in display order.

        Rows selected via the index gutter and rows touched by a cell
        selection both count, so Ctrl+A (all cells) selects every row.
        """
        indices = {r for r in self.sheet.get_selected_rows() if isinstance(r, int)}
        indices.update(cell[0] for cell in self.sheet.get_selected_cells()
                       if isinstance(cell, tuple))
        return [self._rows[i] for i in sorted(indices) if 0 <= i < len(self._rows)]

    def clear_selection(self):
        self.sheet.deselect("all")

    # ---- event routing ----------------------------------------------------
    def _cell(self, event) -> tuple[int | None, int | None]:
        row = self.sheet.identify_row(event=event, allow_end=False)
        col = self.sheet.identify_column(event=event, allow_end=False)
        if col is not None and not (0 <= col < len(self._columns)):
            col = None
        return row, col

    def _on_click(self, event):
        # Shift/Ctrl press = building a selection, not opening a picker.
        if getattr(event, "state", 0) & (0x0001 | 0x0004):
            return
        row, col = self._cell(event)
        if row is None:
            return  # headers and the area under the table do nothing
        if col is not None and self._is_dropdown_cell(col):
            self.sheet.open_dropdown(row, col)

    def _on_double_click(self, event):
        row, col = self._cell(event)
        if row is None or self._is_dropdown_cell(col):
            return  # a constrained cell is edited by its dropdown
        r = self._row_at(row)
        if r is not None:
            self._on_edit(r)

    def _on_right_click(self, event):
        row, _col = self._cell(event)
        if row is None:
            return
        r = self._row_at(row)
        if r is not None:
            self._on_menu(r, event)
