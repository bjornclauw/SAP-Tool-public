"""EditRowDialog — improved work-order edit popup."""

from __future__ import annotations

import logging
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk

from ui import ui_styles
from ui.ui_styles import styled_frame, styled_label
from ui.window_chrome import show_centered

from .models import PICS_FOLDER, STATUS_COLORS, STATUS_LABELS, field_label, is_image_file
from .options import dropdown_lists, load_wo_tree


def show_failure_dialog(parent, row: dict):
    """Read-only window naming the failing SAP script line of an upload."""
    from .row_state import failure_text

    top = tk.Toplevel(parent)
    top.withdraw()
    top.title("Upload error")
    try:
        top.transient(parent)
    except Exception:
        pass
    top.config(bg=ui_styles.COLORS["bg"])
    top.geometry("720x320")
    tk.Label(top, text=f"{row.get('FLOC', '')}  —  {row.get('STEXT', '')[:60]}",
             bg=ui_styles.COLORS["bg"], fg=ui_styles.COLORS["text"],
             font=ui_styles.FONTS["title"]).pack(anchor="w", padx=12, pady=(10, 4))
    box = tk.Text(top, wrap="word", bg=ui_styles.COLORS["surface"],
                  fg=ui_styles.COLORS["error"], relief="solid", bd=1,
                  font=ui_styles.FONTS["body"])
    box.insert("1.0", failure_text(row))
    box.config(state="disabled")
    box.pack(fill="both", expand=True, padx=12, pady=(0, 8))
    tk.Button(top, text="Close", command=top.destroy,
              bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
              relief="solid", bd=1, padx=12, pady=4).pack(pady=(0, 10))
    show_centered(top, ui_styles.SUPPORT_COLOR)


def ask_free_text(parent, title: str, prompt: str, initial: str = "",
                  suggestions=None):
    """A small modal with an editable combo: free text plus suggestions.

    Used for bulk REV, where any custom revision is allowed but the active
    week's register is worth offering. Returns the text, or None if cancelled.
    """
    top = tk.Toplevel(parent)
    top.withdraw()
    top.title(title)
    try:
        top.transient(parent)
    except Exception:
        pass
    top.grab_set()
    top.config(bg=ui_styles.COLORS["bg"])
    top.resizable(False, False)

    styled_label(top, prompt, font=ui_styles.FONTS["body"]).pack(
        anchor="w", padx=12, pady=(12, 4))
    var = tk.StringVar(value=initial)
    combo = ttk.Combobox(top, textvariable=var, values=list(suggestions or []),
                         state="normal", width=28)
    combo.pack(padx=12, fill="x")
    combo.focus_set()

    result: dict[str, str | None] = {"value": None}

    def ok():
        result["value"] = var.get().strip()
        top.destroy()

    btns = styled_frame(top)
    btns.pack(fill="x", padx=12, pady=12)
    tk.Button(btns, text="Apply", command=ok, bg=ui_styles.COLORS["primary"],
              fg="#ffffff", relief="solid", bd=1, padx=12, pady=4,
              cursor="hand2").pack(side="left")
    tk.Button(btns, text="Cancel", command=top.destroy, bg=ui_styles.COLORS["surface"],
              fg=ui_styles.COLORS["text"], relief="solid", bd=1, padx=12, pady=4,
              cursor="hand2").pack(side="left", padx=8)
    top.bind("<Return>", lambda _e: ok())
    top.bind("<Escape>", lambda _e: top.destroy())
    show_centered(top, ui_styles.SUPPORT_COLOR)
    parent.wait_window(top)
    return result["value"]


class _Callout(tk.Frame):
    """A collapsible box: a clickable header reveals the body.

    Used to keep the row's log/meta fields (ids, raw URLs) out of the way
    until they are wanted, so the form fits without a scrollbar.
    """

    def __init__(self, parent, title: str, open_: bool = False, on_toggle=None):
        super().__init__(parent, bg=ui_styles.COLORS["bg"],
                         highlightbackground=ui_styles.COLORS["border"],
                         highlightthickness=1)
        self._open = False
        self._on_toggle = on_toggle
        header = tk.Frame(self, bg=ui_styles.COLORS["surface"], cursor="hand2")
        header.pack(fill="x")
        self._chevron = tk.Label(header, text="\u25b8", bg=ui_styles.COLORS["surface"],
                                 fg=ui_styles.COLORS["text_secondary"],
                                 font=(ui_styles.FONTS["body"][0], 10))
        self._chevron.pack(side="left", padx=(8, 4), pady=4)
        self._title = tk.Label(header, text=title, bg=ui_styles.COLORS["surface"],
                               fg=ui_styles.COLORS["text"],
                               font=ui_styles.FONTS["body"], anchor="w")
        self._title.pack(side="left", pady=4)
        for widget in (header, self._chevron, self._title):
            widget.bind("<Button-1>", self.toggle)
        self.body = tk.Frame(self, bg=ui_styles.COLORS["bg"])
        if open_:
            self.toggle()

    def toggle(self, *_):
        self._open = not self._open
        if self._open:
            self.body.pack(fill="x")
            self._chevron.config(text="\u25be")
        else:
            self.body.pack_forget()
            self._chevron.config(text="\u25b8")
        if self._on_toggle is not None:
            self._on_toggle(self._open)


class EditRowDialog(tk.Toplevel):
    """Left: SAP-style field groups (Location data / Order data / Responsibility
       / Texts / Administration) with validation and copy, plus a collapsed
       Log info callout; the pane scrolls only when it overflows.
       Right: responsive image gallery with zoom lightbox, open folder, download.
       Bottom: Save/Cancel + status pill + keyboard shortcuts (Ctrl+S, Esc)."""

    def __init__(self, parent, row: dict, on_save=None, rev_options: list[str] | None = None,
                 dropdowns: dict[str, list[str]] | None = None,
                 wo_tree: dict[str, list[str]] | None = None, on_open_in_sap=None):
        super().__init__(parent)
        self._row = row
        self._on_save = on_save
        self._on_open_in_sap = on_open_in_sap
        # In-cell-style pickers for the constrained columns: the window supplies
        # its live lists (register + hierarchy); standalone use falls back to
        # the option file so the dialog is testable on its own.
        self._dropdowns = self._resolve_dropdowns(dropdowns, rev_options)
        self._wo_tree = dict(wo_tree) if wo_tree else load_wo_tree()
        self.title(f"Edit — {row.get('FLOC','')} / {row.get('STEXT','')[:30]}")
        self.withdraw()
        self.transient(parent)
        self.grab_set()
        self.config(bg=ui_styles.COLORS["bg"])
        self.geometry("1080x720")
        self.minsize(900, 500)

        self._thumb_refs: list[tk.PhotoImage] = []
        self._entries: dict[str, tk.StringVar] = {}
        self._text_widgets: dict[str, tk.Text] = {}
        self._combos: dict[str, ttk.Combobox] = {}
        self._error_var = tk.StringVar()
        # Gallery auto-fetch: a missing/corrupt cached image is fetched once
        # when the modal opens, so the thumbnails appear without a click.
        self._auto_fetch_tried: set[str] = set()
        self._auto_download_running = False

        # Header strip — status pill + id + floc
        self._build_header()
        self._build_failure_note()

        pw = tk.PanedWindow(self, orient="horizontal", bg=ui_styles.COLORS["bg"],
                            sashrelief="groove", sashwidth=5)
        pw.pack(fill="both", expand=True, padx=6, pady=6)

        left = styled_frame(pw)
        pw.add(left, width=540, minsize=380)
        self._build_form(left)

        right = styled_frame(pw)
        pw.add(right, width=420, minsize=300)
        self._build_gallery(right)

        self._build_footer()

        self.bind("<Control-s>", lambda e: self._do_save())
        self.bind("<Escape>", lambda e: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        show_centered(self, ui_styles.SUPPORT_COLOR)
        # focus first editable
        self.after(100, lambda: self._focus_first())

    def _focus_first(self):
        for k in ("FLOC", "STEXT"):
            if k in self._entries:
                try:
                    # find entry widget via inner? fallback
                    pass
                except Exception:
                    pass
                break

    # ---- failure detail ---------------------------------------------------
    def _build_failure_note(self):
        """Name the failing SAP line when the row's last upload errored."""
        from .row_state import failure_text, has_failure
        if not has_failure(self._row):
            return
        tk.Label(self, text=failure_text(self._row), justify="left", anchor="w",
                 bg=ui_styles.COLORS["error_light"], fg=ui_styles.COLORS["error"],
                 font=(ui_styles.FONTS["body"][0], 8), padx=10, pady=6).pack(
                     fill="x", padx=6, pady=(4, 0))

    # ---- header -----------------------------------------------------------
    def _build_header(self):
        bar = tk.Frame(self, bg=ui_styles.COLORS["surface"], highlightbackground=ui_styles.COLORS["border"], highlightthickness=1)
        bar.pack(fill="x", padx=6, pady=(6, 0), ipady=6)
        code = self._row.get("Status", "0")
        color = STATUS_COLORS.get(code, "#888888")
        label = STATUS_LABELS.get(code, code)
        pill = tk.Label(bar, text=f" ●  {code} · {label} ", bg=color, fg="white",
                        font=(ui_styles.FONTS["body"][0], 9, "bold"), padx=8, pady=2)
        pill.pack(side="left", padx=8)
        tk.Label(bar, text=f"{self._row.get('FLOC','')}  —  {self._row.get('STEXT','')[:40]}",
                 bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
                 font=ui_styles.FONTS["title"]).pack(side="left", padx=12)
        # id + copy
        id_val = self._row.get("_id", "")
        def copy_id():
            self.clipboard_clear()
            self.clipboard_append(id_val)
            # brief feedback
            pill.config(text=" Copied ID! ", bg="#27ae60")
            self.after(1200, lambda: pill.config(text=f" ●  {code} · {label} ", bg=color))
        tk.Button(bar, text="Copy ID", command=copy_id,
                  bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text_secondary"],
                  relief="solid", bd=1, cursor="hand2", font=(ui_styles.FONTS["body"][0], 8)).pack(side="right", padx=8)
        tk.Label(bar, text=id_val[:16], bg=ui_styles.COLORS["surface"],
                 fg=ui_styles.COLORS["text_secondary"], font=(ui_styles.FONTS["body"][0], 8)).pack(side="right")

    # ---- form -------------------------------------------------------------
    def _build_form(self, parent):
        # The form scrolls only when it must (e.g. the Log info callout is
        # expanded on a short window); the scrollbar hides itself otherwise.
        canvas = tk.Canvas(parent, bg=ui_styles.COLORS["bg"], highlightthickness=0)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        self._form_canvas = canvas
        self._form_vsb = vsb
        self._form_scrollbar_shown = False

        inner = styled_frame(canvas)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>",
                    lambda e: (canvas.itemconfig(win, width=e.width),
                               self._sync_form_scrollbar()))
        inner.bind("<Configure>", lambda e: self._sync_form_scrollbar())

        groups = [
            ("Location data", ["FLOC", "WP", "PG"], ["FLOC"]),
            ("Order data", ["WO_TYPE", "WO_KEY", "PRIOR", "USTATUS", "REV"], []),
            ("Responsibility", ["USER"], []),
            ("Texts", ["STEXT", "LTEXT"], ["STEXT"]),
            ("Administration", ["WO", "Bericht"], []),
        ]
        for index, (group_name, keys, required) in enumerate(groups):
            self._section_header(inner, group_name, first=index == 0)
            for key in keys:
                self._build_field(inner, key, key in required)
            if group_name == "Administration":
                self._callout = _Callout(inner, "Log info",
                                         on_toggle=self._on_callout_toggle)
                self._callout.pack(fill="x", padx=ui_styles.SPACING["comfortable"],
                                   pady=(ui_styles.SPACING["normal"], ui_styles.SPACING["normal"]))
                self._build_log_callout(self._callout.body)

        inner.update_idletasks()
        self._sync_wo_key_values()
        self._bind_form_wheel(inner)
        self._sync_form_scrollbar()

    def _section_header(self, parent, title: str, first: bool = False):
        pad_top = ui_styles.SPACING["tight"] if first else ui_styles.SPACING["normal"]
        header = styled_frame(parent)
        header.pack(fill="x", padx=ui_styles.SPACING["comfortable"], pady=(pad_top, 2))
        tk.Frame(header, width=3, bg=ui_styles.COLORS["primary"]).pack(
            side="left", fill="y", padx=(0, 6))
        styled_label(header, title.upper(), font=("Segoe UI", 8, "bold"),
                     fg=ui_styles.COLORS["primary"]).pack(side="left")
        tk.Frame(parent, height=1, bg=ui_styles.COLORS["border_light"]).pack(
            fill="x", padx=ui_styles.SPACING["comfortable"], pady=(0, ui_styles.SPACING["tight"]))

    def _build_field(self, parent, key: str, is_required: bool):
        val = str(self._row.get(key, ""))
        rowf = styled_frame(parent)
        rowf.pack(fill="x", padx=ui_styles.SPACING["comfortable"], pady=3)
        styled_label(rowf, field_label(key) + (" *" if is_required else ""),
                     font=ui_styles.FONTS["body"], fg=ui_styles.COLORS["text"],
                     width=17, anchor="w").pack(side="left")
        if key == "LTEXT":
            txt = tk.Text(rowf, height=4, wrap="word",
                          bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
                          relief="solid", bd=1, font=ui_styles.FONTS["body"])
            txt.insert("1.0", val)
            txt.pack(side="left", fill="x", expand=True, padx=(6, 0))
            self._text_widgets[key] = txt
            if is_required:
                txt.bind("<KeyRelease>", lambda e, k=key: self._validate_field(k))
            return
        if key == "REV":
            self._build_rev_field(rowf, key, val)
            return
        var = tk.StringVar(value=val)
        self._entries[key] = var
        if self._dropdowns.get(key):
            # Constrained field: a read-only picker mirroring the grid. The
            # row's current value stays selectable even if the list has changed.
            opts = list(self._dropdowns[key])
            if val and val not in opts:
                opts.insert(0, val)
            cb = ttk.Combobox(rowf, textvariable=var, values=opts, state="readonly")
            cb.pack(side="left", fill="x", expand=True, padx=(6, 0))
            self._combos[key] = cb
            if key == "WO_TYPE":
                var.trace_add("write", self._sync_wo_key_values)
            return
        ent = tk.Entry(rowf, textvariable=var,
                       bg=ui_styles.COLORS["surface"] if not (is_required and not val.strip())
                       else ui_styles.COLORS["warning_light"],
                       fg=ui_styles.COLORS["text"], relief="solid", bd=1,
                       font=ui_styles.FONTS["body"])
        ent.pack(side="left", fill="x", expand=True, padx=(6, 0))
        if is_required:
            var.trace_add("write", lambda *_ , k=key: self._validate_field(k))
        self._copy_button(rowf, var)

    def _build_rev_field(self, rowf, key: str, val: str):
        original = str(self._row.get("_original_rev", ""))
        opts = list(self._dropdowns.get("REV", []))
        # A blank first entry = "no revision"; then the row's own values, so
        # they stay selectable even if the register has since changed.
        if "" not in opts:
            opts.insert(0, "")
        for candidate in (val, original):
            if candidate and candidate not in opts:
                opts.insert(1, candidate)
        var = tk.StringVar(value=val)
        self._entries[key] = var
        # Editable: the register is a suggestion list, not a closed set. The
        # bulk action already accepts a free-text revision.
        cb = ttk.Combobox(rowf, textvariable=var, values=opts, state="normal")
        cb.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self._combos[key] = cb
        if original and original != val:
            tk.Button(rowf, text="Restore", command=lambda v=var, o=original: v.set(o),
                      bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["primary"],
                      activebackground=ui_styles.COLORS["surface_hover"],
                      relief="flat", bd=0, cursor="hand2",
                      font=ui_styles.FONTS["small"]).pack(side="left", padx=(6, 0))
            from .revisions import is_year_upgrade
            if self._row.get("_auto_assigned"):
                note = "Auto-assigned from"
            elif is_year_upgrade(original, val):
                note = "Year-upgraded from"
            else:
                note = "Original revision"
            styled_label(rowf.master, f"{note} {original}", font=ui_styles.FONTS["small"],
                         fg=ui_styles.COLORS["primary"]).pack(
                             anchor="w", padx=(ui_styles.SPACING["comfortable"] + 122, 0))

    def _copy_button(self, parent, var: tk.StringVar):
        tk.Button(parent, text="Copy", command=lambda v=var: self._copy_var(v),
                  bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text_secondary"],
                  activebackground=ui_styles.COLORS["surface_hover"],
                  relief="flat", bd=0, cursor="hand2",
                  font=ui_styles.FONTS["small"]).pack(side="left", padx=(6, 0))

    # ---- form scrolling ---------------------------------------------------
    def _scroll_form_to_end(self):
        canvas = getattr(self, "_form_canvas", None)
        try:
            if canvas is not None and canvas.winfo_exists():
                canvas.yview_moveto(1.0)
        except tk.TclError:
            pass

    def _sync_form_scrollbar(self):
        """Show the scrollbar only while the form overflows its pane."""
        canvas = getattr(self, "_form_canvas", None)
        vsb = getattr(self, "_form_vsb", None)
        if canvas is None or vsb is None or not canvas.winfo_exists():
            return
        try:
            if canvas.winfo_height() <= 1:
                return
            box = canvas.bbox("all")
            canvas.configure(scrollregion=box or (0, 0, 0, 0))
            overflow = bool(box) and box[3] > canvas.winfo_height() + 2
        except tk.TclError:
            return
        if overflow and not self._form_scrollbar_shown:
            self._form_vsb.pack(side="right", fill="y")
            self._form_scrollbar_shown = True
        elif not overflow and self._form_scrollbar_shown:
            self._form_vsb.pack_forget()
            self._form_scrollbar_shown = False

    def _bind_form_wheel(self, widget):
        """Wheel scrolls the form from anywhere inside it, so it never changes
        the value of whichever dropdown happens to be under the pointer."""
        widget.bind("<MouseWheel>", self._on_form_wheel)
        for child in widget.winfo_children():
            self._bind_form_wheel(child)

    def _on_form_wheel(self, event):
        canvas = getattr(self, "_form_canvas", None)
        if canvas is not None:
            try:
                canvas.yview_scroll(-1 * (event.delta // 120), "units")
            except tk.TclError:
                pass
        return "break"

    def _on_callout_toggle(self, opened: bool):
        self.update_idletasks()
        self._sync_form_scrollbar()
        if opened:
            # Bring the freshly revealed content into view.
            self.update_idletasks()
            self._scroll_form_to_end()

    def _build_log_callout(self, body):
        styled_label(body, "Picture URLs", font=ui_styles.FONTS["small"],
                     fg=ui_styles.COLORS["text_secondary"]).pack(
                         anchor="w", padx=ui_styles.SPACING["comfortable"],
                         pady=(ui_styles.SPACING["normal"], 2))
        pics = str(self._row.get("PICS", ""))
        pics_box = tk.Frame(body, bg=ui_styles.COLORS["surface"],
                            highlightbackground=ui_styles.COLORS["border"], highlightthickness=1)
        pics_box.pack(fill="x", padx=ui_styles.SPACING["comfortable"], pady=2)
        txt = tk.Text(pics_box, height=3, wrap="word",
                      bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text_secondary"],
                      relief="flat", font=ui_styles.FONTS["small"])
        txt.insert("1.0", pics or "(no images)")
        txt.config(state="disabled")
        txt.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        tk.Button(pics_box, text="Open folder", command=self._open_pics_folder,
                  bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["primary"],
                  relief="solid", bd=1, cursor="hand2").pack(side="right", padx=4)
        # extra meta fields (Bericht is shown in the Administration group)
        for extra in ["_id", "tableId", "PREV_ORDER_ID", "PICS_REP"]:
            v = str(self._row.get(extra, ""))
            if not v:
                continue
            rowf = styled_frame(body)
            rowf.pack(fill="x", padx=ui_styles.SPACING["comfortable"], pady=2)
            styled_label(rowf, field_label(extra), font=ui_styles.FONTS["small"], width=17,
                         anchor="w", fg=ui_styles.COLORS["text_secondary"]).pack(side="left")
            var = tk.StringVar(value=v)
            self._entries[extra] = var
            tk.Entry(rowf, textvariable=var,
                     bg=ui_styles.COLORS["bg"], fg=ui_styles.COLORS["text_secondary"],
                     relief="solid", bd=1, font=ui_styles.FONTS["small"]).pack(
                         side="left", fill="x", expand=True, padx=(6, 0))
            self._copy_button(rowf, var)
        tk.Frame(body, height=ui_styles.SPACING["normal"], bg=ui_styles.COLORS["bg"]).pack()

    # ---- dropdowns --------------------------------------------------------
    @staticmethod
    def _resolve_dropdowns(dropdowns, rev_options):
        """The picker lists each constrained field offers.

        Starts from the shared option lists; a caller (the window) overrides
        with its live lists.
        """
        resolved = dropdown_lists(rev_options)
        for key, values in (dropdowns or {}).items():
            resolved[key] = list(values)
        return resolved

    def _sync_wo_key_values(self, *_):
        """Narrow the WO_KEY picker to the keys the chosen WO_TYPE owns
        (mirrors the grid; falls back to every key when the type is unknown)."""
        cb = self._combos.get("WO_KEY")
        wt_var = self._entries.get("WO_TYPE")
        if cb is None or wt_var is None:
            return
        allowed = self._wo_tree.get(str(wt_var.get()).strip()) or self._dropdowns.get("WO_KEY", [])
        cb.config(values=list(allowed))

    def _validate_field(self, key: str):
        """Clear the required-field message once both required fields are filled."""
        entries = getattr(self, "_entries", {})
        if all(str(entries[k].get()).strip()
               for k in ("FLOC", "STEXT") if k in entries):
            self._error_var.set("")

    def _copy_var(self, var: tk.StringVar):
        self.clipboard_clear()
        self.clipboard_append(var.get())

    def _open_pics_folder(self):
        try:
            os.makedirs(PICS_FOLDER, exist_ok=True)
            os.startfile(PICS_FOLDER)  # type: ignore[attr-defined]
        except AttributeError:
            import subprocess
            subprocess.call(["xdg-open", PICS_FOLDER])

    # ---- gallery ----------------------------------------------------------
    def _build_gallery(self, parent):
        hdr = styled_frame(parent)
        hdr.pack(fill="x", pady=(0, 4))
        styled_label(hdr, "Images", font=ui_styles.FONTS["title"]).pack(side="left")
        tk.Button(hdr, text="Open folder", command=self._open_pics_folder,
                  bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["primary"],
                  relief="solid", bd=1, cursor="hand2", font=(ui_styles.FONTS["body"][0], 8)).pack(side="right", padx=4)
        tk.Button(hdr, text="↻ Refresh", command=lambda: self._refresh_gallery(parent),
                  bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text_secondary"],
                  relief="solid", bd=1, cursor="hand2", font=(ui_styles.FONTS["body"][0], 8)).pack(side="right")

        self._gallery_parent = parent
        urls = self._gallery_image_urls()

        if not urls:
            styled_label(parent, "No images", fg=ui_styles.COLORS["text_secondary"],
                         font=ui_styles.FONTS["body"]).pack(anchor="w", pady=20)
            styled_label(parent, "PICS and PICS_REP are empty — check BCsys upload.",
                         fg=ui_styles.COLORS["text_secondary"], font=(ui_styles.FONTS["body"][0], 8)).pack(anchor="w")
            return

        canvas = tk.Canvas(parent, bg=ui_styles.COLORS["surface"], highlightthickness=0)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg=ui_styles.COLORS["surface"])
        win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_canvas_conf(e):
            canvas.itemconfig(win, width=canvas.winfo_width())
            self._layout_gallery(inner, urls)
        canvas.bind("<Configure>", _on_canvas_conf)

        # initial layout
        inner.update_idletasks()
        self._layout_gallery(inner, urls)
        inner.update_idletasks()
        canvas.configure(scrollregion=canvas.bbox("all"))
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        # store for refresh
        self._gallery_inner = inner
        self._gallery_urls = urls
        self._gallery_canvas = canvas
        self._schedule_auto_download(urls)

    def _refresh_gallery(self, parent):
        # simple re-download check
        logging.getLogger("sap_import").info("Refresh gallery")
        # clear and rebuild
        for ch in parent.winfo_children():
            ch.destroy()
        self._thumb_refs.clear()
        self._build_gallery(parent)

    def _gallery_image_urls(self) -> list[str]:
        """Every image URL on the row, from PICS and PICS_REP, in order, unique."""
        urls: list[str] = []
        for field in ("PICS", "PICS_REP"):
            for raw in str(self._row.get(field, "") or "").split(","):
                url = raw.strip()
                if url and url not in urls:
                    urls.append(url)
        return urls

    @staticmethod
    def _load_thumbnail(filepath, size):
        """A Tk thumbnail for a cached image, or None if it won't decode."""
        if not os.path.exists(filepath):
            return None
        try:
            from PIL import Image, ImageTk

            with Image.open(filepath) as pil_img:
                pil_img.thumbnail((size - 12, size - 12), Image.LANCZOS)
                return ImageTk.PhotoImage(pil_img)
        except Exception:
            return None

    def _layout_gallery(self, inner, urls):
        # clear previous rows
        for ch in list(inner.winfo_children()):
            ch.destroy()
        w = inner.winfo_width() or 340
        thumb = 150
        cols = max(1, w // (thumb + 8))
        row_frame = None
        for idx, url in enumerate(urls):
            if idx % cols == 0:
                row_frame = tk.Frame(inner, bg=ui_styles.COLORS["surface"])
                row_frame.pack(fill="x", pady=2)
            cell = tk.Frame(row_frame, bg="white", highlightbackground=ui_styles.COLORS["border"],
                            highlightthickness=1, width=thumb, height=thumb+24)
            cell.pack(side="left", padx=4, pady=4)
            cell.pack_propagate(False)

            filename = url.rstrip("/").split("/")[-1]
            filepath = os.path.join(PICS_FOLDER, filename)

            # A cached file that will not decode (e.g. a truncated or wrongly
            # downloaded attachment) is treated as missing, so clicking it
            # re-downloads instead of showing an undecodable placeholder.
            tk_img = self._load_thumbnail(filepath, thumb)
            if tk_img is not None:
                self._thumb_refs.append(tk_img)
                lbl = tk.Label(cell, image=tk_img, bg="white", cursor="hand2")
                lbl.pack(expand=True, fill="both")
                lbl.bind("<Button-1>", lambda e, fp=filepath: self._open_lightbox(fp))
            else:
                txt = f"⬇ {filename[:18]}"
                lbl = tk.Label(cell, text=txt, fg="#e67e22",
                               bg="white", font=ui_styles.FONTS["body"], cursor="hand2", wraplength=thumb-8, justify="center")
                lbl.pack(expand=True, fill="both", padx=4, pady=4)
                lbl.bind("<Button-1>", lambda e, fp=filepath, u=url: self._on_missing_click(fp, u))

            # filename bar
            bar = tk.Frame(cell, bg=ui_styles.COLORS["bg"], height=20)
            bar.pack(fill="x", side="bottom")
            bar.pack_propagate(False)
            tk.Label(bar, text=filename[:20], bg=ui_styles.COLORS["bg"],
                     fg=ui_styles.COLORS["text_secondary"], font=(ui_styles.FONTS["body"][0], 7)).pack(side="left", padx=4)
            dot = "●" if tk_img is not None else "○"
            col = "#27ae60" if tk_img is not None else "#e67e22"
            tk.Label(bar, text=dot, bg=ui_styles.COLORS["bg"], fg=col, font=(ui_styles.FONTS["body"][0], 8)).pack(side="right", padx=4)

    def _missing_gallery_downloads(self, urls) -> list[tuple[str, str]]:
        """The (url, filepath) pairs with no usable cached image, each once.

        A file that exists but won't decode counts as missing, so a stale cache
        is replaced rather than shown as an unreadable thumbnail.
        """
        missing = []
        for url in urls:
            if url in self._auto_fetch_tried:
                continue
            filepath = os.path.join(PICS_FOLDER, url.rstrip("/").split("/")[-1])
            if is_image_file(filepath):
                continue
            self._auto_fetch_tried.add(url)
            missing.append((url, filepath))
        return missing

    def _schedule_auto_download(self, urls):
        """Fetch any missing/corrupt cached image once, off the UI thread.

        A restored row never came through this session's fetch, and a stale
        cache can be unreadable, so without this the thumbnails only appear
        after a manual click.
        """
        missing = self._missing_gallery_downloads(urls)
        if not missing or self._auto_download_running:
            return

        self._auto_download_running = True
        result: queue.Queue = queue.Queue()

        def worker():
            for url, filepath in missing:
                self._download_file(url, filepath)
            result.put(True)

        threading.Thread(target=worker, daemon=True).start()
        self._poll_auto_download(result)

    def _poll_auto_download(self, result):
        try:
            result.get_nowait()
        except queue.Empty:
            if self.winfo_exists():
                self.after(150, lambda: self._poll_auto_download(result))
            return
        self._auto_download_running = False
        if self.winfo_exists():
            self._refresh_gallery(getattr(self, "_gallery_parent", self))

    @staticmethod
    def _download_file(url, filepath) -> bool:
        try:
            from urllib.request import Request, urlopen
            os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
            req = Request(url, headers={"User-Agent": "Mozilla/4.0 (compatible; MSIE 6.0)"})
            with urlopen(req, timeout=30) as r, open(filepath, "wb") as f:
                f.write(r.read())
            return True
        except Exception as e:
            logging.getLogger("sap_import").warning("download failed %s: %s", url, e)
            return False

    def _on_missing_click(self, filepath, url):
        if self._download_file(url, filepath):
            self._refresh_gallery(getattr(self, "_gallery_parent", self))
            return
        try:
            os.startfile(filepath)  # type: ignore[attr-defined]
        except Exception:
            pass

    def _open_lightbox(self, filepath):
        if not os.path.exists(filepath):
            return
        top = tk.Toplevel(self)
        top.title(os.path.basename(filepath))
        top.config(bg="black")
        top.geometry("900x700")
        try:
            from PIL import Image, ImageTk
            pil = Image.open(filepath)
            # fit to 880x660
            pil.thumbnail((880, 660), Image.LANCZOS)
            img = ImageTk.PhotoImage(pil)
            # keep ref
            top._img_ref = img  # type: ignore[attr-defined]
            tk.Label(top, image=img, bg="black", cursor="hand2").pack(expand=True, fill="both")
            tk.Label(top, text=f"{os.path.basename(filepath)}  —  click to close, right-click to open folder",
                     bg="black", fg="white", font=(ui_styles.FONTS["body"][0], 8)).pack(side="bottom", pady=4)
            top.bind("<Button-1>", lambda e: top.destroy())
            top.bind("<Button-3>", lambda e: (self._open_pics_folder(), top.destroy()))
        except Exception:
            tk.Label(top, text=filepath, bg="black", fg="white").pack(expand=True)
            top.bind("<Button-1>", lambda e: top.destroy())

    # ---- footer -----------------------------------------------------------
    def _build_footer(self):
        bar = styled_frame(self)
        bar.pack(fill="x", padx=ui_styles.SPACING["comfortable"], pady=(8, 8))
        def do_save():
            self._do_save()
        tk.Button(bar, text="✓ Save  (Ctrl+S)", command=do_save,
                  bg=ui_styles.COLORS["primary"], fg="#ffffff", relief="solid", bd=1,
                  activebackground=ui_styles.COLORS.get("primary_hover", "#2980b9"),
                  padx=12, pady=4, cursor="hand2").pack(side="left", padx=(0, 8))
        tk.Button(bar, text="Cancel  (Esc)", command=self.destroy,
                  bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["text"],
                  relief="solid", bd=1, padx=12, pady=4, cursor="hand2").pack(side="left")
        tk.Label(bar, textvariable=self._error_var,
                 bg=ui_styles.COLORS["bg"], fg=ui_styles.COLORS["error"],
                 font=(ui_styles.FONTS["body"][0], 8)).pack(side="left", padx=(10, 0))
        tk.Label(bar, text="LTEXT is multiline · PICS opens folder",
                 bg=ui_styles.COLORS["bg"], fg=ui_styles.COLORS["text_secondary"],
                 font=(ui_styles.FONTS["body"][0], 8)).pack(side="right")
        self._open_sap_btn = tk.Button(bar, text="Open in SAP", command=self._do_open_in_sap,
                                       bg=ui_styles.COLORS["surface"], fg=ui_styles.COLORS["primary"],
                                       relief="solid", bd=1, padx=12, pady=4, cursor="hand2",
                                       state="normal" if str(self._row.get("SAP_NUMBER", "")).strip()
                                       else "disabled")
        self._open_sap_btn.pack(side="right", padx=(0, 10))

    def _do_open_in_sap(self):
        if self._on_open_in_sap is not None:
            self._on_open_in_sap(self._row)

    def _collect(self) -> dict:
        values = {key: var.get() for key, var in self._entries.items()}
        for key, txt in self._text_widgets.items():
            values[key] = txt.get("1.0", "end-1c")
        return values

    def _do_save(self) -> bool:
        """Validate, apply and close. False (modal stays open) when a required
        field is empty, so an incomplete row can never be saved silently."""
        values = self._collect()
        missing = [key for key in ("FLOC", "STEXT")
                   if not str(values.get(key, self._row.get(key, ""))).strip()]
        if missing:
            self._error_var.set(f"{' and '.join(missing)} required — cannot save")
            return False
        self._error_var.set("")

        rev_before = str(self._row.get("REV", ""))
        for key, value in values.items():
            self._row[key] = value
        if str(self._row.get("REV", "")) != rev_before:
            # A deliberate choice or restore is left alone by auto-assign.
            self._row["_auto_assigned"] = False
            self._row["_auto_assign_skip"] = True
        if self._on_save:
            self._on_save()
        self.destroy()
        return True
