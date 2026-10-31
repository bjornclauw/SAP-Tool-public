"""Tokenizes SAP GUI Scripting text into Command objects, and evaluates
individual expressions (quoted literals, {{engine_vars}}, and local-scope
identifiers) at execution time.

This is intentionally a small, regex-based parser -- not a full VBScript
grammar. It covers every pattern used in the existing button scripts (see
REWORK_PLAN.md's "Configuration Demo" section):

    session.findById("...").text = "..."      -> SET_PROPERTY
    session.findById("...").sendVKey 0        -> CALL_METHOD
    session.findById("...").sendVKey(0)       -> CALL_METHOD
    session.findById("...").press             -> CALL_METHOD (no args)
    session.findById("...").ExtractData       -> CALL_METHOD (no args)
    tempval = {{workorder}}                   -> ASSIGN_VAR (local, script-scoped)
    tempval2 = "PROC OK"                      -> ASSIGN_VAR (local, script-scoped)
    {{ workorder }} = tempval2                -> ENGINE_VAR_ASSIGN (writes the shared namespace)
    If {{var}} = "x" Then / ElseIf / Else / End If  -> IF / ELSEIF / ELSE / ENDIF
    On Error Resume Next / On Error GoTo 0    -> ON_ERROR_RESUME / ON_ERROR_STOP

If you need a genuinely new syntax shape, add a case to parse_line() rather
than trying to make one regex do everything -- that's the lesson from the old
1115-line engine.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_FINDBY_RE = re.compile(r'^session\.findById\(\s*"(.+?)"\s*\)\.(\w+)\s*(.*)$', re.IGNORECASE)
_ENGINE_VAR_ASSIGN_RE = re.compile(r"^\{\{\s*(\w+)\s*\}\}\s*=\s*(.+)$")
_ASSIGN_FROM_METHOD_RE = re.compile(
    r'^(\w+)\s*=\s*session\.findById\(\s*"(.+?)"\s*\)\.(\w+)\s+(.+)$',
    re.IGNORECASE,
)
_PLAIN_ASSIGN_RE = re.compile(r"^(\w+)\s*=\s*(.+)$")
_RUN_SCRIPT_RE = re.compile(r'^run_script\(\s*"(.+?)"\s*\)$')
_ON_ERROR_RESUME_RE = re.compile(r"^on\s+error\s+resume\s+next$", re.IGNORECASE)
_ON_ERROR_STOP_RE = re.compile(r"^on\s+error\s+go\s*to\s*0$", re.IGNORECASE)
_IF_RE = re.compile(r"^if\s+(.+?)\s+then$", re.IGNORECASE)
_ELSEIF_RE = re.compile(r"^elseif\s+(.+?)\s+then$", re.IGNORECASE)
_ELSE_RE = re.compile(r"^else$", re.IGNORECASE)
_ENDIF_RE = re.compile(r"^end\s*if$", re.IGNORECASE)
_CONDITION_RE = re.compile(r"^\s*(.+?)\s*(<>|=)\s*(.+?)\s*$")
_READ_PROPERTY_RE = re.compile(
    r'^(\w+)\s*=\s*session\.findById\(\s*"(.+?)"\s*\)\.(\w+)\s*$',
    re.IGNORECASE,
)
_SESSION_READ_RE = re.compile(r"^(\w+)\s*=\s*session\.([\w.]+)\s*$", re.IGNORECASE)
_ENGINE_VAR_TOKEN_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_DIGIT_RUN_RE = re.compile(r"\d+")

# Leaders that sneak in when an SAP number is pasted from mail, Word or
# Excel: bullets, dashes, quote/bracket wrappers. Letters are deliberately
# NOT in here -- stripping those would eat real input.
_PASTE_LEADERS = " \t\r\n•◦▪○●∙·-–—−*#>»«›‹\"'‘’“”()[]{}:;,."


def sanitize_sap_number(value: str, digits: bool = True) -> str:
    """Paste-hardening for SAP number entry fields.

    Pasted values arrive with whitespace padding, bullet leaders or
    trailing prose ("• 40001234", "4000 1234", "40001234 pump defect").
    Cleaning: drop all whitespace, strip leading paste leaders, then
    (digits=True) return the first digit run. When there is no digit run
    -- or digits=False (free codes like the revisie field) -- the
    leader-stripped text comes back untouched, so nothing the user typed
    deliberately is ever destroyed.
    """
    if not isinstance(value, str) or not value:
        return value
    scrubbed = re.sub(r"\s+", "", value).strip(_PASTE_LEADERS)
    if not digits:
        return scrubbed
    match = _DIGIT_RUN_RE.search(scrubbed)
    return match.group(0) if match else scrubbed


@dataclass
class Command:
    line_no: int  # 1-based line number (breakpoints/UI use this)
    raw: str  # original source line, shown verbatim in the debugger
    kind: str  # ENGINE_VAR_ASSIGN | ASSIGN_VAR | SET_PROPERTY | CALL_METHOD | ASSIGN_FROM_METHOD | READ_SESSION | RUN_SCRIPT | COMMENT | BLANK | UNKNOWN
    target: str | None = None  # findById path, or a plain variable name, or button_id for RUN_SCRIPT
    member: str | None = None  # property/method name
    value_expr: str | None = None  # unresolved right-hand side
    args_expr: str | None = None  # unresolved method argument
    breakpoint: bool = False


def parse_line(line_no: int, raw: str) -> Command:
    stripped = raw.strip()

    if not stripped:
        return Command(line_no, raw, "BLANK")
    if stripped.startswith("#") or stripped.startswith("'"):
        return Command(line_no, raw, "COMMENT")

    m = _ON_ERROR_RESUME_RE.match(stripped)
    if m:
        return Command(line_no, raw, "ON_ERROR_RESUME")
    if _ON_ERROR_STOP_RE.match(stripped):
        return Command(line_no, raw, "ON_ERROR_STOP")

    m = _IF_RE.match(stripped)
    if m:
        return Command(line_no, raw, "IF", value_expr=m.group(1).strip())
    m = _ELSEIF_RE.match(stripped)
    if m:
        return Command(line_no, raw, "ELSEIF", value_expr=m.group(1).strip())
    if _ELSE_RE.match(stripped):
        return Command(line_no, raw, "ELSE")
    if _ENDIF_RE.match(stripped):
        return Command(line_no, raw, "ENDIF")

    m = _ENGINE_VAR_ASSIGN_RE.match(stripped)
    if m:
        return Command(
            line_no, raw, "ENGINE_VAR_ASSIGN", target=m.group(1), value_expr=m.group(2).strip()
        )

    m = _ASSIGN_FROM_METHOD_RE.match(stripped)
    if m:
        return Command(
            line_no, raw, "ASSIGN_FROM_METHOD",
            target=m.group(1),
            value_expr=m.group(2),
            member=m.group(3),
            args_expr=m.group(4).strip(),
        )

    m = _FINDBY_RE.match(stripped)
    if m:
        path, member, remainder = m.group(1), m.group(2), m.group(3).strip()
        if remainder.startswith("="):
            return Command(
                line_no,
                raw,
                "SET_PROPERTY",
                target=path,
                member=member,
                value_expr=remainder[1:].strip(),
            )
        if remainder.startswith("(") and remainder.endswith(")"):
            return Command(
                line_no,
                raw,
                "CALL_METHOD",
                target=path,
                member=member,
                args_expr=remainder[1:-1].strip() or None,
            )
        return Command(
            line_no, raw, "CALL_METHOD", target=path, member=member, args_expr=remainder or None
        )

    m = _READ_PROPERTY_RE.match(stripped)
    if m:
        return Command(
            line_no, raw, "READ_PROPERTY",
            target=m.group(1),
            value_expr=m.group(2),
            member=m.group(3),
        )

    m = _SESSION_READ_RE.match(stripped)
    if m:
        return Command(
            line_no, raw, "READ_SESSION",
            target=m.group(1),
            member=m.group(2),
        )

    m = _PLAIN_ASSIGN_RE.match(stripped)
    if m:
        return Command(line_no, raw, "ASSIGN_VAR", target=m.group(1), value_expr=m.group(2).strip())

    m = _RUN_SCRIPT_RE.match(stripped)
    if m:
        return Command(line_no, raw, "RUN_SCRIPT", target=m.group(1))

    return Command(line_no, raw, "UNKNOWN")


def parse_script(script_text: str) -> list[Command]:
    lines = script_text.splitlines()
    return [parse_line(i + 1, line) for i, line in enumerate(lines)]


def evaluate_expr(expr: str | None, engine, locals_scope: dict) -> str:
    """Resolve one right-hand-side expression to a plain string.

    Handles, in order: a quoted literal, a bare {{engine_var}}, a bare local
    identifier, and (as a fallback) inline {{var}} substitution inside a
    longer string. Extend this function -- not the engine -- if you need a
    richer expression language later.
    """
    if expr is None:
        return ""
    expr = expr.strip()

    if len(expr) >= 2 and expr[0] == '"' and expr[-1] == '"':
        inner = expr[1:-1]
        def _sub_q(m: re.Match) -> str:
            return engine.get_variable(m.group(1))
        return _ENGINE_VAR_TOKEN_RE.sub(_sub_q, inner)

    m = re.fullmatch(r"\{\{\s*(\w+)\s*\}\}", expr)
    if m:
        return engine.get_variable(m.group(1))

    if re.fullmatch(r"\w+", expr):
        if expr in locals_scope:
            return locals_scope[expr]
        # Unknown bare word (e.g. a numeric literal like "0"): pass through as-is.
        return engine.get_variable(expr, default=expr)

    def _sub(match: re.Match) -> str:
        return engine.get_variable(match.group(1))

    return _ENGINE_VAR_TOKEN_RE.sub(_sub, expr)


def evaluate_condition(expr: str | None, engine, locals_scope: dict) -> bool:
    """Evaluate an ``If`` condition: ``<operand> = <operand>`` or ``<>``.

    Operands are quoted literals, ``{{engine_var}}`` or local identifiers --
    the same shapes evaluate_expr already handles. Comparison is exact on the
    resolved strings. Anything unparseable is False (never crashes a run).
    """
    m = _CONDITION_RE.match(expr or "")
    if not m:
        return False
    left = evaluate_expr(m.group(1), engine, locals_scope)
    right = evaluate_expr(m.group(3), engine, locals_scope)
    if m.group(2) == "=":
        return left == right
    return left != right
