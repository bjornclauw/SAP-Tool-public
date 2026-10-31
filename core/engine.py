"""SapScriptEngine -- the single interpreter for SAP GUI Scripting.

One engine, one execution path. Always uses a worker thread for COM calls.

Design rules:
- All business logic lives in the *script*, not here. This file only knows
  how to parse a script into Commands and execute them one at a time.
- One instance is shared by the toolbar and every tool window, so there is
  exactly one variable namespace for the whole app.
- The engine ALWAYS executes line-by-line. The debugger doesn't change how
  execution works -- it just pauses between calls to step(). Non-debug mode
  is simply "call step() in a loop without stopping" (see run_immediate).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from .commands import Command, evaluate_condition, evaluate_expr, parse_script

logger = logging.getLogger("sap_engine")


class SapScriptEngine:
    def __init__(self, backend_factory: Callable | None = None):
        self.backend_factory = backend_factory

        # Always create worker (async mode). With no factory the engine talks
        # to the fake COM layer, so a bare SapScriptEngine() runs headlessly.
        from .sap_worker import SapWorker

        if backend_factory is not None:
            self.worker = SapWorker(backend_factory)
        else:
            from .com_backend import RealSapBackend
            from .fake_com import FakeComClient

            self.worker = SapWorker(lambda: RealSapBackend(app_factory=FakeComClient()))

        # Flat variable namespace shared by every UI component and every script.
        self.variables: dict[str, str] = {}
        self._var_watchers: dict[str, list[Callable[[str], None]]] = {}

        # Currently loaded script -- what step()/continue_execution() operate on.
        self.commands: list[Command] = []
        self.raw_script: str = ""
        self.pointer: int = 0
        self.locals_scope: dict[str, str] = {}
        self.last_result = None
        self.last_extracted_data = None
        self._just_hit_breakpoint = False  # Track if we just stopped at a breakpoint

        self.step_callbacks: list[Callable[[int, Command], None]] = []
        self.log_callbacks: list[Callable[[str], None]] = []
        self.populate_grid_callbacks: list[Callable[[str, object], None]] = []
        self.busy_callbacks: list[Callable[[bool], None]] = []
        self.run_state_callbacks: list[Callable[[bool], None]] = []  # whole run
        self.run_script_callbacks: list[Callable[[str], None]] = []  # For run_script() command
        self._paused = False  # Track if engine is paused (waiting for debugger)
        self._pending_debug_script: tuple[str, str, str] | None = None


        # Async state
        self._pending_job_id: int | None = None
        self._pending_cmd: Command | None = None
        self._continue_mode = False
        self._run_immediate_mode = False

        # Connection lifetime: attach for the duration of a run, release when
        # the outermost run ends (see begin_run / end_run) -- like the Excel's
        # per-macro Attach_Session. Attach is lazy (the worker attaches before
        # the first command). A debug session holds the attach for its own
        # lifetime via _debug_hold. Nothing is retained between runs.
        self._run_depth = 0
        self._debug_hold = False

        self.process_events: Callable[[], None] | None = None
        self._last_error: str | None = None
        self._last_error_line: int | None = None
        self._last_error_raw: str | None = None
        # On Error Resume Next: while set, a failing command is skipped instead
        # of stopping the run.
        self._error_tolerant = False
        # If/ElseIf/Else/End If jump tables, rebuilt on every load().
        self._branch_next: dict[int, int] = {}
        self._endif_of: dict[int, int] = {}
        # Indices of the commands actually executed, in order, so step_back()
        # walks the taken path (skipped If branches are never revisited).
        self._history: list[int] = []

    def last_error_detail(self) -> tuple[str, int | None, str] | None:
        """The last run's failure: (message, line number, source line).

        None when the last run succeeded or none has run."""
        if not self._last_error:
            return None
        return (self._last_error, self._last_error_line, self._last_error_raw or "")

    # ---- backend / connection -------------------------------------------------
    def connect(self) -> bool:
        return self.worker.connect()

    def is_connected(self) -> bool:
        return self.worker.is_connected()

    # ---- variables --------------------------------------------------------
    def get_variable(self, name: str, default: str = "") -> str:
        return self.variables.get(name, default)

    def set_variable(self, name: str, value: str) -> None:
        self.variables[name] = value
        for cb in list(self._var_watchers.get(name, [])):
            cb(value)

    def watch_variable(self, name: str, callback: Callable[[str], None]) -> None:
        self._var_watchers.setdefault(name, []).append(callback)

    def unwatch_variable(self, name: str, callback: Callable[[str], None]) -> None:
        """Remove a previously registered variable watcher. No-op when the
        watcher is not registered -- tool windows call this on destroy so a
        closed window stops receiving updates from the shared engine."""
        cbs = self._var_watchers.get(name)
        if not cbs:
            return
        try:
            cbs.remove(callback)
        except ValueError:
            pass
        if not cbs:
            self._var_watchers.pop(name, None)

    def on_run_script(self, callback: Callable[[str], None]) -> None:
        """Register a callback for run_script() commands in scripts.

        The callback receives the button_id string and should execute that
        button's script (e.g., via ScriptRunnerMixin.run_script).
        """
        self.run_script_callbacks.append(callback)

    def resume(self):
        """Resume execution after being paused."""
        self._paused = False
        # Execute the pending RUN_SCRIPT command
        if self.commands and self.pointer < len(self.commands):
            cmd = self.commands[self.pointer]
            if cmd.kind == "RUN_SCRIPT":
                assert cmd.target is not None
                for cb in self.run_script_callbacks:
                    cb(cmd.target)
                self.pointer += 1
            self._begin_step()

    # ---- loading / running --------------------------------------------------
    def load(self, script_text: str) -> None:
        self.raw_script = script_text
        self.commands = parse_script(script_text)
        self.pointer = 0
        self.locals_scope = {}
        self._error_tolerant = False
        self._history = []
        self._build_blocks()
        self._log(f"LOAD ({len(self.commands)} lines)")

    def _build_blocks(self) -> None:
        """Match If/ElseIf/Else/End If into jump tables (nested-aware)."""
        self._branch_next = {}
        self._endif_of = {}
        stack: list[list[int]] = []
        for idx, cmd in enumerate(self.commands):
            if cmd.kind == "IF":
                stack.append([idx])
            elif cmd.kind in ("ELSEIF", "ELSE"):
                if stack:
                    stack[-1].append(idx)
            elif cmd.kind == "ENDIF":
                if not stack:
                    continue
                branches = stack.pop()
                for b_i, b in enumerate(branches):
                    nxt = branches[b_i + 1] if b_i + 1 < len(branches) else idx
                    self._branch_next[b] = nxt
                    self._endif_of[b] = idx

    def _jump_to_next_branch(self, start: int) -> None:
        """A false If/ElseIf: run the next branch that matches, or End If."""
        current = start
        while True:
            nxt = self._branch_next.get(current)
            if nxt is None:
                self.pointer = current + 1
                return
            branch = self.commands[nxt]
            if branch.kind == "ELSEIF":
                if evaluate_condition(branch.value_expr, self, self.locals_scope):
                    self.pointer = nxt + 1
                    return
                current = nxt
                continue
            if branch.kind == "ELSE":
                self.pointer = nxt + 1
                return
            # End If: no branch matched -- step onto End If to close the block.
            self.pointer = nxt
            return

    def run_immediate(self, script_text: str) -> None:
        self._last_error = None
        self._last_error_line = None
        self._last_error_raw = None
        self.load(script_text)
        self._run_immediate_mode = True
        self.begin_run()
        try:
            self._begin_step()
            self.wait_until_idle()
        finally:
            self.end_run()

    def step(self) -> Command | None:
        self._continue_mode = False
        self._run_immediate_mode = False
        self._begin_step()
        return self.commands[self.pointer - 1] if self.pointer > 0 else None

    def continue_execution(self) -> None:
        self._continue_mode = True
        self.begin_run()
        try:
            self._begin_step()
            self.wait_until_idle()
        finally:
            self.end_run()

    def stop(self) -> None:
        self.pointer = 0
        self._continue_mode = False
        self._run_immediate_mode = False
        self._history = []
        self._log("STOPPED")

    def reset(self) -> None:
        self.commands = []
        self.raw_script = ""
        self.pointer = 0
        self.locals_scope = {}
        self._continue_mode = False
        self._run_immediate_mode = False
        self._error_tolerant = False
        self._history = []
        self._last_error = None
        self._last_error_line = None
        self._last_error_raw = None
        self._log("RESET")

    def step_back(self) -> None:
        """Move back to the previously executed line (taken path only).

        Uses the execution history, so with If/Else blocks this lands on the
        line that actually ran, never on a skipped branch. Re-running happens
        on the next step() call. Clamped at the start of the script."""
        self._just_hit_breakpoint = False  # Clear so breakpoint will be checked again
        if self._history:
            self.pointer = self._history.pop()
            self._log(f"STEP BACK -> line {self.pointer + 1}")
        else:
            self._log("Already at the start of the script")

    def toggle_breakpoint(self, line_index: int) -> None:
        if 0 <= line_index < len(self.commands):
            self.commands[line_index].breakpoint = not self.commands[line_index].breakpoint

    def clear_breakpoints(self) -> None:
        for cmd in self.commands:
            cmd.breakpoint = False

    # ---- the one execution path --------------------------------------------
    @property
    def is_busy(self) -> bool:
        return self._pending_job_id is not None

    # ---- run boundaries (connection lifetime) -----------------------------
    def begin_run(self) -> None:
        """Enter a run. Attach is lazy (the worker attaches before the first
        command), so this only tracks nesting: a nested run_script shares the
        outer run's attach."""
        self._run_depth += 1
        if self._run_depth == 1:
            self._fire_run_state(True)

    def end_run(self) -> None:
        """Leave a run; release the attach once the outermost run ends, unless
        a debug session is holding it."""
        if self._run_depth > 0:
            self._run_depth -= 1
        if self._run_depth == 0:
            self._fire_run_state(False)
        self._maybe_release()

    def _fire_run_state(self, running: bool) -> None:
        for cb in self.run_state_callbacks:
            cb(running)

    def start_debug_session(self) -> None:
        """Hold the attach for the lifetime of a debug session (debugging
        steps between pauses, so the session must not release)."""
        self._debug_hold = True

    def end_debug_session(self) -> None:
        """The debug session ended; release the attach if no run is live."""
        self._debug_hold = False
        self._maybe_release()

    def release_session(self) -> None:
        """Release the SAP session now, regardless of run/debug state (the
        debugger's Stop/Reset). The next command re-attaches."""
        self.worker.disconnect()

    def probe_attach(self) -> None:
        """Attach once and release immediately, to report whether SAP is
        reachable (the startup label probe) without holding anything. The
        result arrives through the worker's connect event."""
        self.worker.schedule_connect()
        self.worker.disconnect()

    def _maybe_release(self) -> None:
        """Release the SAP session when nothing needs it: no run in flight and
        no debug session holding it. The next command re-attaches."""
        if self._run_depth == 0 and not self._debug_hold:
            self.release_session()

    def _begin_step(self):
        if self._pending_job_id is not None:
            return

        if self.pointer >= len(self.commands):
            self._log("END OF SCRIPT")
            self._continue_mode = False
            self._run_immediate_mode = False
            if self._debug_hold and self._run_depth <= 1:
                # A debug run that reached the end releases now; the debugger
                # window may stay open, and the next step re-attaches lazily.
                self.release_session()
            return

        cmd = self.commands[self.pointer]

        # If we just hit a breakpoint and user clicked Continue, execute this line
        if self._just_hit_breakpoint:
            self._just_hit_breakpoint = False
            # Don't set continue_mode=True here - let the caller (step/continue) decide
            # Fall through to execute the command (skip breakpoint check)
        elif self._continue_mode and cmd.breakpoint:
            # Check breakpoint BEFORE executing (only in continue mode)
            self._continue_mode = False
            self._just_hit_breakpoint = True
            self._log(f"BREAKPOINT at line {self.pointer + 1}")
            for cb in self.step_callbacks:
                cb(self.pointer, cmd)
            return

        self._execute_current_command(cmd)

    def poll(self):
        for result in self.worker.poll():
            if result.id != self._pending_job_id:
                continue
            self._finish_step(result)

    def _finish_step(self, result):
        cmd = self._pending_cmd
        if cmd is None:
            return
        if result.ok:
            self.last_result = result.value
            if cmd.kind in ("READ_PROPERTY", "READ_SESSION"):
                assert cmd.target is not None
                value = str(result.value) if result.ok else ""
                self.locals_scope[cmd.target] = value
                src = f"{cmd.value_expr}.{cmd.member}" if cmd.value_expr else f"session.{cmd.member}"
                self._log(f"{cmd.target} = {value!r} (read from {src})")
                self.pointer += 1
                self._pending_job_id = None
                self._pending_cmd = None
                self._fire_busy(False)
                self._after_step(cmd)
                return
            if cmd.kind == "ASSIGN_FROM_METHOD":
                assert cmd.target is not None
                value = str(result.value) if result.ok else ""
                self.locals_scope[cmd.target] = value
                self._log(f"{cmd.target} = {value!r} (from {cmd.value_expr}.{cmd.member})")
                # Fall through to pointer increment and common cleanup
            if cmd.member == "ExtractData":
                # SAP grid data is cached here so the following PopulateGridView
                # call can hand it to Python: ExtractData -> PopulateGridView ->
                # populate_grid_callbacks(grid_name, rows). SAP has no for-loop;
                # iterating the rows is always the Python side's job.
                self.last_extracted_data = result.value
            if cmd.member == "PopulateGridView" and cmd.args_expr is not None:
                grid_name = evaluate_expr(cmd.args_expr, self, self.locals_scope)
                for cb in self.populate_grid_callbacks:
                    cb(grid_name, self.last_extracted_data)
                # The backend's call result *is* the export file path (the
                # fake writes it; real SAP returns the grid name). No reaching
                # into worker._backend.
                export_file = self.last_result
                if export_file:
                    self.set_variable("export_done", "1")
                    self.set_variable("export_path", str(Path(export_file).parent))
                    self.set_variable("export_file", Path(export_file).name)
            resolved_args = self._resolve_args(cmd.args_expr)
            self._log(f"CALL {cmd.target}.{cmd.member}({resolved_args}) -> ok")
            self.pointer += 1
        else:
            self._log(f"ERROR at line {cmd.line_no}: {result.error}")
            if self._error_tolerant:
                # On Error Resume Next: skip the failing line and carry on.
                self.pointer += 1
            else:
                self._continue_mode = False
                self._run_immediate_mode = False
                self._last_error = result.error
                self._last_error_line = cmd.line_no
                self._last_error_raw = cmd.raw

        self._pending_job_id = None
        self._pending_cmd = None
        self._fire_busy(False)
        self._after_step(cmd)

    def _execute_current_command(self, cmd):
        """Execute the command at the current pointer and advance."""
        self._history.append(self.pointer)
        if cmd.kind in ("COMMENT", "BLANK"):
            self.pointer += 1
            self._after_step(cmd)
            return

        if cmd.kind == "ON_ERROR_RESUME":
            self._error_tolerant = True
            self.pointer += 1
            self._after_step(cmd)
            return

        if cmd.kind == "ON_ERROR_STOP":
            self._error_tolerant = False
            self.pointer += 1
            self._after_step(cmd)
            return

        if cmd.kind == "IF":
            if evaluate_condition(cmd.value_expr, self, self.locals_scope):
                self.pointer += 1
            else:
                self._jump_to_next_branch(self.pointer)
            self._after_step(cmd)
            return

        if cmd.kind in ("ELSEIF", "ELSE"):
            # Reached by falling out of a true branch: this control line is not
            # on the taken path, so drop it and step onto End If.
            if self._history:
                self._history.pop()
            endif = self._endif_of.get(self.pointer)
            self.pointer = endif if endif is not None else self.pointer + 1
            self._after_step(cmd)
            return

        if cmd.kind == "ENDIF":
            self.pointer += 1
            self._after_step(cmd)
            return

        if cmd.kind == "RUN_SCRIPT":
            if self._run_immediate_mode:
                # Non-debug: fire callbacks synchronously, chain runs now
                for cb in self.run_script_callbacks:
                    cb(cmd.target)
                self.pointer += 1
                self._after_step(cmd)
            else:
                # Debug mode: pause for debugger
                self._paused = True
                self._pending_debug_script = (cmd.target, cmd.target, "")
                self._log(f"PAUSED at RUN_SCRIPT: {cmd.target}")
                for cb in self.log_callbacks:
                    cb(f"[PAUSED] Close debugger to run: {cmd.target}")
            return

        if cmd.kind == "ENGINE_VAR_ASSIGN":
            value = evaluate_expr(cmd.value_expr, self, self.locals_scope)
            self.set_variable(cmd.target, value)
            self._log(f"{{{{ {cmd.target} }}}} = {value!r}")
            self.pointer += 1
            self._after_step(cmd)
            return

        if cmd.kind == "ASSIGN_VAR":
            value = evaluate_expr(cmd.value_expr, self, self.locals_scope)
            self.locals_scope[cmd.target] = value
            self._log(f"{cmd.target} = {value!r}  (local)")
            self.pointer += 1
            self._after_step(cmd)
            return

        if cmd.kind == "READ_PROPERTY":
            self._pending_cmd = cmd
            self._fire_busy(True)
            self._pending_job_id = self.worker.submit(
                "get_property", path=cmd.value_expr, member=cmd.member,
            )
            return

        if cmd.kind == "READ_SESSION":
            self._pending_cmd = cmd
            self._fire_busy(True)
            self._pending_job_id = self.worker.submit(
                "get_session_property", member=cmd.member,
            )
            return

        # Async: submit to worker
        self._pending_cmd = cmd
        self._fire_busy(True)
        if cmd.kind == "SET_PROPERTY":
            value = evaluate_expr(cmd.value_expr, self, self.locals_scope)
            self._pending_job_id = self.worker.submit(
                "set_property", path=cmd.target, member=cmd.member, value=value
            )
        elif cmd.kind == "CALL_METHOD":
            args = self._resolve_args_list(cmd.args_expr) if cmd.args_expr else []
            self._pending_job_id = self.worker.submit(
                "call_method", path=cmd.target, member=cmd.member, args=args
            )

        elif cmd.kind == "ASSIGN_FROM_METHOD":
            args = self._resolve_args_list(cmd.args_expr) if cmd.args_expr else []
            self._pending_cmd = cmd
            self._fire_busy(True)
            self._pending_job_id = self.worker.submit(
                "call_method", path=cmd.value_expr, member=cmd.member, args=args
            )
        else:
            self._log(f"UNPARSED line {cmd.line_no}: {cmd.raw}")
            self.pointer += 1
            self._fire_busy(False)
            self._after_step(cmd)

    def _after_step(self, cmd):
        for cb in self.step_callbacks:
            cb(self.pointer - 1, cmd)
        if self._continue_mode:
            self._begin_step()
        elif self._run_immediate_mode:
            self._begin_step()

    def _fire_busy(self, busy: bool):
        for cb in self.busy_callbacks:
            cb(busy)

    def wait_until_idle(self, timeout: float = 30.0) -> None:
        start = time.time()
        while time.time() - start < timeout:
            self.poll()
            if self._pending_job_id is None:
                return
            if self.process_events:
                self.process_events()
            time.sleep(0.01)

    def _resolve_args_list(self, args_expr: str | None) -> list:
        """Resolve method arguments into a list, splitting on commas.
        Quoted args stay strings; unquoted numeric-looking args become int."""
        if not args_expr:
            return []
        args = self._split_args(args_expr)
        resolved = []
        for arg in args:
            stripped = arg.strip()
            if stripped.startswith('"') and stripped.endswith('"'):
                inner = stripped[1:-1]
                val = evaluate_expr(inner, self, self.locals_scope)
                resolved.append(val)
            else:
                val = evaluate_expr(stripped, self, self.locals_scope)
                try:
                    resolved.append(int(val))
                except ValueError:
                    resolved.append(val)
        return resolved

    def _resolve_args(self, args_expr: str | None) -> str:
        """Resolve method arguments into a display string (for logging)."""
        resolved = self._resolve_args_list(args_expr)
        quoted = []
        for v in resolved:
            if isinstance(v, str) and v.startswith('"') and v.endswith('"'):
                quoted.append(v)
            elif isinstance(v, str):
                quoted.append(f'"{v}"')
            else:
                quoted.append(str(v))
        return ", ".join(quoted)

    @staticmethod
    def _split_args(args_expr: str) -> list[str]:
        """Split comma-separated args, respecting quoted strings."""
        args = []
        current = []
        in_quotes = False
        i = 0
        s = args_expr
        while i < len(s):
            ch = s[i]
            if ch == '"':
                in_quotes = not in_quotes
                current.append(ch)
            elif ch == ',' and not in_quotes:
                args.append(''.join(current))
                current = []
            else:
                current.append(ch)
            i += 1
        if current:
            args.append(''.join(current))
        return args

    def _log(self, message: str) -> None:
        logger.info(message)
        for cb in self.log_callbacks:
            cb(message)
