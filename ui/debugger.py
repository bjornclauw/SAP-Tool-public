"""DebuggerWindow -- a floating Toplevel that lets you single-step a script
against a live (or mock) SAP session.

Deliberately decoupled from SapToolbar: it only needs a reference to the
engine, so any tool window could open its own debugger the same way. It
never runs SAP calls itself -- it only calls engine.step() / .continue_
execution() / .stop() / .reset() and re-renders based on engine state.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from core.commands import parse_line

from . import ui_styles
from .ui_styles import styled_button, styled_frame, styled_label, styled_text


class DebuggerWindow(tk.Toplevel):
    def __init__(self, parent, engine, titlebar_color=None):
        super().__init__(parent)
        self.title("Script Debugger")
        self.geometry("800x520")
        self.engine = engine
        self._previous_grab: tk.Toplevel | None = None
        self.engine.step_callbacks.append(self._on_step)
        self.engine.log_callbacks.append(self._on_log)

        # Handle window close to process pending scripts
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Set title bar color
        if titlebar_color is None:
            titlebar_color = ui_styles.COLORS["debug"]

        # Hide window, centre + style it, then show (prevents flicker)
        from .window_chrome import center_on_toolbar, correct_for_decorations

        self.withdraw()
        self.update_idletasks()
        self.minsize(640, 420)  # Can't shrink below initial size, but can grow
        center_on_toolbar(self, self.master)
        self.deiconify()
        correct_for_decorations(self, self.master)
        self._set_titlebar_color(titlebar_color)

        self._build_script_view()
        self._build_controls()
        self._build_log()

    def _set_titlebar_color(self, color):
        """Set Windows title bar color."""
        from .window_chrome import apply_titlebar_color

        apply_titlebar_color(self, color)

    # ---- layout --------------------------------------------------------
    def _build_script_view(self):
        style = ttk.Style()
        style.configure("DebugTreeview.Treeview", font=ui_styles.FONTS["mono"])
        container = styled_frame(self)
        container.pack(
            side="top", fill="both", expand=True,
            padx=ui_styles.SPACING["normal"],
            pady=ui_styles.SPACING["normal"],
        )
        v_scroll = ttk.Scrollbar(container, orient="vertical")
        h_scroll = ttk.Scrollbar(container, orient="horizontal")
        self.tree = ttk.Treeview(
            container, columns=(), show="tree",
            style="DebugTreeview.Treeview",
            yscrollcommand=v_scroll.set,
            xscrollcommand=h_scroll.set,
        )
        v_scroll.config(command=self.tree.yview)
        h_scroll.config(command=self.tree.xview)
        self.tree.grid(row=0, column=0, sticky="nsew")
        v_scroll.grid(row=0, column=1, sticky="ns")
        h_scroll.grid(row=1, column=0, sticky="ew")
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)
        self.tree.tag_configure("breakpoint", background=ui_styles.COLORS["debug_breakpoint"])
        self.tree.tag_configure("pending", background=ui_styles.COLORS["debug_active_bg"])  # Light blue
        self.tree.tag_configure(
            "bp_and_pending", background=ui_styles.COLORS["debug_breakpoint_active"]
        )
        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<Double-1>", self._on_double_click)

    def _build_controls(self):
        bar = styled_frame(self)
        bar.pack(side="top", fill="x", padx=ui_styles.SPACING["normal"])
        styled_button(bar, "Step \u2191", command=self._step_back).pack(side="left", padx=(0, 4))
        styled_button(bar, "Step \u2193", command=self._step).pack(side="left", padx=(0, 4))
        styled_button(bar, "Continue", command=self._continue).pack(side="left", padx=(0, 4))
        styled_button(bar, "Stop", command=self._stop, fg=ui_styles.COLORS["error"]).pack(
            side="left", padx=(0, 4)
        )
        styled_button(bar, "Reset", command=self._reset).pack(side="left", padx=(0, 4))
        styled_button(bar, "Clear Breakpoints", command=self._clear_breakpoints).pack(
            side="left", padx=(0, 4)
        )
        self.counter_label = styled_label(bar, "0/0", fg=ui_styles.COLORS["text_secondary"])
        self.counter_label.pack(side="right")

    def _build_log(self):
        container = styled_frame(self)
        container.pack(
            side="bottom", fill="x",
            padx=ui_styles.SPACING["normal"],
            pady=ui_styles.SPACING["normal"],
        )
        h_scroll = ttk.Scrollbar(container, orient="horizontal")
        self.log_text = styled_text(
            container, height=12, state="disabled",
            font=ui_styles.FONTS["mono"],
            wrap="none",
            xscrollcommand=h_scroll.set,
        )
        h_scroll.config(command=self.log_text.xview)
        self.log_text.pack(side="top", fill="x")
        h_scroll.pack(side="bottom", fill="x")

    # ---- public API --------------------------------------------------------
    def load_script(self, script_text: str, title: str = "", on_save=None, on_success=None):
        self.title(f"Script Debugger \u2014 {title}" if title else "Script Debugger")
        self._on_save = on_save
        self._on_success = on_success
        self.engine.load(script_text)
        self._refresh_tree()

    def _on_close(self):
        """Handle window close: destroy window, resume engine, restore modal grab, process pending scripts."""
        # Restore previous modal grab if it still exists
        if self._previous_grab:
            try:
                if self._previous_grab.winfo_exists():
                    self._previous_grab.grab_set()
            except tk.TclError:
                pass

        # Fire on_success if the script finished (all commands executed)
        script_finished = (
            self.engine.commands
            and self.engine.pointer >= len(self.engine.commands)
        )
        on_success = self._on_success if script_finished else None

        self.close()
        self.destroy()

        if on_success:
            on_success()

        # Resume engine if it was paused waiting for debugger
        if self.engine._paused:
            self.engine.resume()

        # Process any pending scripts
        toolbar = self.master
        if hasattr(toolbar, '_process_pending_debug_script'):
            toolbar._process_pending_debug_script()

    def close(self):
        """Clean up by removing engine callbacks and ending the debug hold
        on the SAP attach (release happens here, once, for every close path)."""
        self.engine.end_debug_session()
        if self.engine.step_callbacks:
            try:
                self.engine.step_callbacks.remove(self._on_step)
            except ValueError:
                pass
        if self.engine.log_callbacks:
            try:
                self.engine.log_callbacks.remove(self._on_log)
            except ValueError:
                pass

    # ---- engine callbacks --------------------------------------------------
    def _on_step(self, index, cmd):
        # Guard: only refresh if widget still exists
        try:
            self._refresh_tree()
        except tk.TclError:
            pass

    def _on_log(self, message):
        try:
            self.log_text.config(state="normal")
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        except tk.TclError:
            # Widget may have been destroyed; ignore
            pass

    # ---- control buttons --------------------------------------------------------
    def _step_back(self):
        self.engine.step_back()
        self._refresh_tree()

    def _step(self):
        self.engine.step()

    def _continue(self):
        self.engine.continue_execution()

    def _stop(self):
        self.engine.stop()
        self.engine.release_session()
        self._refresh_tree()

    def _reset(self):
        self.engine.reset()
        self.engine.release_session()
        self._refresh_tree()

    def _clear_breakpoints(self):
        self.engine.clear_breakpoints()
        self._refresh_tree()

    def _on_right_click(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        self.engine.toggle_breakpoint(int(item))
        self._refresh_tree()

    def _on_double_click(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        idx = int(item)
        if idx < 0 or idx >= len(self.engine.commands):
            return
        self._edit_line_popup(idx, self.engine.commands[idx].raw)

    def _edit_line_popup(self, idx: int, current_text: str):
        popup = tk.Toplevel(self)
        popup.title(f"Edit Line {idx + 1}")
        popup.geometry("560x160")
        popup.resizable(False, False)
        popup.transient(self)
        popup.grab_set()
        popup.config(bg=ui_styles.COLORS["bg"])
        try:
            import pywinstyles
            pywinstyles.change_header_color(popup, color=ui_styles.COLORS["debug"])
            pywinstyles.change_title_color(popup, color="white")
        except Exception:
            pass

        text = tk.Text(popup, height=3, font=ui_styles.FONTS["mono"],
                       wrap="none", bg="white", fg=ui_styles.COLORS["text"])
        text.insert("1.0", current_text)
        text.pack(fill="x", padx=8, pady=(8, 4))
        text.focus_set()
        text.tag_add("sel", "1.0", "end")

        def save():
            new_raw = text.get("1.0", "end-1c")
            if new_raw == current_text:
                popup.destroy()
                return
            new_cmd = parse_line(idx + 1, new_raw)
            new_cmd.breakpoint = self.engine.commands[idx].breakpoint
            self.engine.commands[idx] = new_cmd
            self._refresh_tree()
            popup.destroy()
            if self._on_save:
                full = "\n".join(c.raw for c in self.engine.commands)
                self._on_save(full)

        def on_key(ev):
            if ev.keysym == "Return" and ev.state & 0x0004:
                save()
                return "break"
            if ev.keysym == "Escape":
                popup.destroy()
                return "break"

        text.bind("<KeyPress>", on_key)

        bf = styled_frame(popup)
        bf.pack(fill="x", padx=8, pady=(0, 8))
        styled_button(bf, "Save", command=save).pack(side="left", padx=(0, 4))
        styled_button(bf, "Cancel", command=popup.destroy).pack(side="left")

    # ---- rendering --------------------------------------------------------
    def _refresh_tree(self):
        try:
            self.tree.delete(*self.tree.get_children())
        except tk.TclError:
            return
        for i, cmd in enumerate(self.engine.commands):
            if cmd.breakpoint and i == self.engine.pointer:
                tags = ["bp_and_pending"]
            elif cmd.breakpoint:
                tags = ["breakpoint"]
            elif i == self.engine.pointer:
                tags = ["pending"]
            else:
                tags = []
            self.tree.insert("", "end", iid=str(i), text=f"{i + 1}: {cmd.raw}", tags=tags)
        total = len(self.engine.commands)
        self.counter_label.config(text=f"{min(self.engine.pointer, total)}/{total}")
