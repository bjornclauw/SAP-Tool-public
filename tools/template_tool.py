"""TemplateToolWindow -- copy this file for every new tool window.

    cp tools/template_tool.py tools/your_tool.py

Then:
  1. Rename the class and the config keys below.
  2. Add matching entries under customcomponents.buttons / .textinput in
     config/toolbar_config.json (var_name + default_value + script).
  3. Register it in SapToolbar._build_menu() (see AGENTS.md).

That's the whole checklist. You do NOT need to:
  - write trace_add anywhere
  - call engine.set_variable yourself
  - write your own debug-mode branch
  - write your own "sync entries before running" method
  - duplicate _open_debugger

All of that lives in ScriptRunnerMixin (custom_widgets.py), which every
BaseToolWindow already has. This file shows the two calls you actually
write: bind_entry() for fields, run_script() for buttons.

If your window has a grid: put the Treeview plus its horizontal scrollbar
in their own container (bar packed FIRST with side="bottom", tree after
with fill/expand), and pass manage_scrollbar=False to GridColumnManager.
A manager-packed bar lands as the pane's last child, where the packer
squeezes it out on short windows -- exactly when scrolling is needed.
See PurchaseRequestHelperWindow._build_lookup_grid for the pattern.
"""

from __future__ import annotations

from core import config as cfg
from ui import ui_styles
from ui.custom_widgets import BaseToolWindow, CustomButton, CustomEntry, CustomLabel
from ui.ui_styles import styled_frame


class TemplateToolWindow(BaseToolWindow):
    def __init__(self, parent, engine):
        # Add an entry for this tool in ui_styles.TOOL_COLORS and use it here,
        # so its titlebar follows the window-colour convention
        # (see ui/window_chrome.py). It centres on the toolbar automatically.
        super().__init__(parent, engine,
                         titlebar_color=ui_styles.TOOL_COLORS.get(
                             "your_tool", ui_styles.SUPPORT_COLOR))
        self.title("Template Tool")

        self._build_form()
        self._build_status_bar()  # provided by BaseToolWindow

        self._show()

    def _build_form(self):
        row = styled_frame(self)
        row.pack(
            fill="x",
            padx=ui_styles.SPACING["comfortable"],
            pady=ui_styles.SPACING["comfortable"],
        )

        header_cfg = cfg.get_header_cfg(self.config_data, "template_field")
        CustomLabel(
            row,
            "headers",
            "template_field",
            text=header_cfg.get("text", "Some Field"),
            font=ui_styles.FONTS["header"],
        ).pack(anchor="w")

        # --- ONE LINE per field: reads var_name/default_value from config,
        # wires UI<->engine both ways, and registers it to be auto-synced
        # before every run_script() call. Never touch trace_add directly. ---
        self.field_var = self.bind_entry("template_field")

        entry_row = styled_frame(row)
        entry_row.pack(anchor="w", pady=(ui_styles.SPACING["tight"], 0))

        entry = CustomEntry(
            entry_row, "textinput", "template_field", textvariable=self.field_var, width=20
        )
        entry.pack(side="left")
        entry.bind("<Return>", lambda e: self._run())

        btn_cfg = cfg.get_button_cfg(self.config_data, "template_btn")
        CustomButton(
            entry_row,
            "buttons",
            "template_btn",
            text=btn_cfg.get("text", "Run"),
            command=self._run,
            bg=ui_styles.COLORS["primary"],
            fg="#ffffff",
            activebackground=ui_styles.COLORS["primary_hover"],
        ).pack(side="left", padx=(ui_styles.SPACING["normal"], 0))

    def _run(self):
        # --- ONE LINE to launch the script: reads it from config, force-syncs
        # every bound field, opens the debugger if debug mode is on, otherwise
        # runs immediately and reports a default "Ran '...'" status. ---
        self.run_script("template_btn")

        # Need custom behaviour after a successful (non-debug) run instead of
        # the default status message? Pass on_success instead:
        #   self.run_script("template_btn", on_success=self._after_run)
        # `on_success` only fires on an immediate (non-debug) run -- debug
        # mode always stops at the debugger, by design (see AGENTS.md).

    # def _after_run(self):
    #     result = self.engine.get_variable("some_output_var")
    #     self.report_status(f"Got: {result}", "success")
