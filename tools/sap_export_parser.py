"""SapExportParser -- parses SAP tab-delimited export files into (headers, rows).

Pure logic, no UI dependencies. Handles the SAP export format:
- Line 1: Date + title metadata
- Line 2: (empty)
- Line 3: Column headers (tab-separated)
- Line 4: Empty lines with '*' markers
- After markers: Actual data rows

Usage:
    parser = SapExportParser()
    headers, data_rows = parser.parse(filepath)
"""

from __future__ import annotations

import os


class SapExportParser:
    """Parses SAP tab-delimited export files into structured data.

    Stateless by design (ADR-0001): ``parse`` returns ``(headers, rows)`` and
    ``parse_with_warnings`` returns the same plus human-readable problems
    (duplicate/ambiguous headers and rows wider than the header row).
    """

    def parse(self, filepath: str) -> tuple[list[str], list[list[str]]]:
        """Parse a SAP tab-delimited export file into (headers, data_rows).

        Raises:
            FileNotFoundError: If the file doesn't exist.
            ValueError: If no header line could be found.
        """
        headers, rows, _warnings = self.parse_with_warnings(filepath)
        return headers, rows

    def parse_with_warnings(
        self, filepath: str
    ) -> tuple[list[str], list[list[str]], list[str]]:
        """Parse the export and also return any problems found.

        Returns:
            (headers, data_rows, warnings)
        """
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Export file not found: {filepath}")

        warnings: list[str] = []
        with open(filepath, encoding="utf-8", errors="ignore") as f:
            content = f.read()

        # Do not strip the whole file: the header row may start with the
        # row-number column, and stripping would hide that leading tab.
        lines = content.splitlines()

        # Find header line: tab-separated, >3 fields, not a date/marker line
        header_line_idx = None
        for i, line in enumerate(lines):
            if (
                "\t" in line
                and not line.strip().startswith("*")
                and not line.strip().startswith(("20", "202"))
            ):
                potential_headers = line.strip().split("\t")
                if len(potential_headers) > 3:
                    header_line_idx = i
                    break

        if header_line_idx is None:
            raise ValueError("Could not find column headers in export")

        # Extract headers — count leading empty columns (row-number column
        # from spreadsheet format [1,0], and possibly a second empty column
        # before the first real header).
        raw_headers = lines[header_line_idx].split("\t")
        all_headers = [h.strip() for h in raw_headers]
        n_leading_empty = 0
        for h in all_headers:
            if h == "":
                n_leading_empty += 1
            else:
                break
        all_headers = all_headers[n_leading_empty:]
        # Preserve empty middle columns (data rows have those positions).
        # Name empties with unique IDs so Treeview column references stay valid.
        for i, h in enumerate(all_headers):
            if not h:
                all_headers[i] = f"_spacer_{i}"

        # Duplicate header text makes the name -> index map ambiguous (the
        # second copy would be unreachable). Keep positions, make names unique
        # and report the rename so the user knows why a heading changed.
        seen_headers: set[str] = set()
        for i, h in enumerate(all_headers):
            if h in seen_headers:
                base = h
                n = 2
                while f"{base} ({n})" in seen_headers:
                    n += 1
                unique = f"{base} ({n})"
                all_headers[i] = unique
                warnings.append(
                    f"duplicate column header '{base}' renamed '{unique}'")
                h = unique
            seen_headers.add(h)

        # Collect data rows (after header, skip empty lines and markers).
        # NOTE: list exports indent with leading tabs, so a marker line looks
        # like "\t*\t..." (raw[0] == "") -- check the stripped line, not
        # just the first cell. Fully-empty rows (page breaks, totals
        # padding) are skipped too; tools can further filter on their key
        # column (e.g. PRH requires a non-empty ATB) via GridManager.
        data_rows = []
        for offset, line in enumerate(lines[header_line_idx + 1 :]):
            line_no = header_line_idx + offset + 2  # 1-based file line
            if "\t" not in line:
                continue
            if line.lstrip(" \t\r\n").startswith("*"):
                continue
            raw = line.rstrip("\n").split("\t")
            if not raw:
                continue
            # Strip the row-number prefix only when this row actually has it.
            # Slicing blindly would eat the first real cell on a layout that
            # does not emit leading empties.
            if (n_leading_empty and len(raw) > n_leading_empty
                    and not any(c.strip() for c in raw[:n_leading_empty])):
                cells = raw[n_leading_empty:]
            else:
                cells = raw
            # SAP GUI converts "|" inside cell content to TAB on export,
            # splitting one cell in two (e.g. "URG| tekst" arrives as "URG",
            # " tekst"). The fragment lands in the next column -- typically
            # an empty-header (_spacer_) slot, so later columns stay aligned
            # by luck. Fold it back with "|" (raw join reconstructs the
            # original text) and blank the fragment slot to keep alignment.
            # NOTE: "|" is legitimate content, never a separator here -- it
            # used to be replaced with a space, which silently destroyed
            # data. The Treeview round-trips pipes fine.
            for i, h in enumerate(all_headers):
                if (
                    h.startswith("_spacer_")
                    and 0 < i < len(cells)
                    and cells[i].strip()
                ):
                    cells[i - 1] = cells[i - 1] + "|" + cells[i]
                    cells[i] = ""
            if len(cells) > len(all_headers):
                warnings.append(
                    f"row {line_no}: {len(cells)} cells, expected "
                    f"{len(all_headers)}; extra cells ignored")
                cells = cells[:len(all_headers)]
            columns = [c.strip().replace("\n", " ").replace("\r", " ")
                       for c in cells]
            # SAP omits trailing tabs for empty trailing cells, so rows
            # can be shorter than the header — pad instead of dropping.
            if len(columns) < len(all_headers):
                columns.extend([""] * (len(all_headers) - len(columns)))
            if not any(c.strip() for c in columns):
                continue
            data_rows.append(columns)

        return all_headers, data_rows, warnings
