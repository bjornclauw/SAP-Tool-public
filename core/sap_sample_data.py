"""Sample SAP data -- the single source of truth for the fake COM layer.

Data only. The object graph and resolver live in ``core/fake_com.py``;
behaviour lives in the SAP scripts in ``config/toolbar_config.json``.
``scripts/generate_fake_scenarios.py`` reads these structures to build
``config/fake_sap_scenarios.json``, which the fake loads at runtime.

The old ``mock_sap_simulator`` mixed data, file I/O and COM-shaped
responses in one class; those shapes now belong to the fake, so only the
raw datasets and the scalar/transaction seed remain here.
"""

from __future__ import annotations

# --- grid datasets -----------------------------------------------------------
# Row keys are the SAP column names the scripts read via getCellValue and
# the header names the export file is built from.

# Real ME5A headers -- must match prh_lookup_grid in toolbar_config.json or
# GridManager renders blank columns.
ME5A_PR = [
    {
        "AanvDt.": "24.09.2026",
        "ATB": "100123",
        "Pos.": "10",
        "Lev.dat.": "01.10.2026",
        "S": "",
        "R": "",
        "P": "X",
        "Artikel": "MAT123",
        "Korte tekst": "Bolt M12",
        "Hoeveelh.": "5",
        "HE": "ST",
        "Vest": "1000",
        "Mag.": "0001",
        "Vaste lev.": "",
        "Leveranc.": "Supplier A",
        "Aant. ATBs": "1",
        "BHE": "ST",
        "BestDatum": "24.09.2026",
        "Totl. wrd.": "25.00",
        "Per": "1",
        "Aanvrager": "Jansen",
        "Bew.status": "Vrijgegeven",
    },
    {
        "AanvDt.": "24.09.2026",
        "ATB": "100124",
        "Pos.": "20",
        "Lev.dat.": "05.10.2026",
        "S": "",
        "R": "X",
        "P": "",
        "Artikel": "MAT124",
        "Korte tekst": "Nut M12",
        "Hoeveelh.": "10",
        "HE": "ST",
        "Vest": "1000",
        "Mag.": "0001",
        "Vaste lev.": "",
        "Leveranc.": "Supplier B",
        "Aant. ATBs": "0",
        "BHE": "ST",
        "BestDatum": "24.09.2026",
        "Totl. wrd.": "5.00",
        "Per": "1",
        "Aanvrager": "Pieters",
        "Bew.status": "Aangevraagd",
    },
]

# IW32 IHKD attachment grid rows, keyed by SAP field names for getCellValue
# access, plus display-friendly names used by the export.
IW32_ATTACH_READ = [
    {
        "DOKAR": "PDF", "DOKNR": "10000047177", "FILENAME": "MO_2024_001.pdf",
        "FILE_SIZE": "1.2 MB", "DKTXT": "PROFORMA", "ORGFN": "MO_2024_001.pdf",
        "DOKOBJ": "MO_2024_001.pdf", "DOCUMENT_LINK_ORIGINAL": "MO_2024_001.pdf",
        "FILE_NAME": "MO_2024_001.pdf", "DESCR": "PROFORMA", "TXT": "PROFORMA",
        "Type": "PDF", "Filename": "MO_2024_001.pdf", "Size": "1.2 MB",
        "Metadata": "PROFORMA", "Status": "Uploaded",
    },
    {
        "DOKAR": "DOC", "DOKNR": "10000047178", "FILENAME": "MO_2024_002.docx",
        "FILE_SIZE": "850 KB", "DKTXT": "PROFORMA", "ORGFN": "MO_2024_002.docx",
        "DOKOBJ": "MO_2024_002.docx", "DOCUMENT_LINK_ORIGINAL": "MO_2024_002.docx",
        "FILE_NAME": "MO_2024_002.docx", "DESCR": "PROFORMA", "TXT": "PROFORMA",
        "Type": "Word", "Filename": "MO_2024_002.docx", "Size": "850 KB",
        "Metadata": "PROFORMA", "Status": "Uploaded",
    },
    {
        "DOKAR": "PDF", "DOKNR": "10000047179", "FILENAME": "PO_2024_003.pdf",
        "FILE_SIZE": "2.1 MB", "DKTXT": "PROFORMA", "ORGFN": "PO_2024_003.pdf",
        "DOKOBJ": "PO_2024_003.pdf", "DOCUMENT_LINK_ORIGINAL": "PO_2024_003.pdf",
        "FILE_NAME": "PO_2024_003.pdf", "DESCR": "PROFORMA", "TXT": "PROFORMA",
        "Type": "PDF", "Filename": "PO_2024_003.pdf", "Size": "2.1 MB",
        "Metadata": "PROFORMA", "Status": "Uploaded",
    },
]

# IW32 attachment export columns.
IW32_ATTACH_EXPORT = [
    {"Type": "PDF", "Filename": "MO_2024_001.pdf", "Size": "1.2 MB",
     "Metadata": "PROFORMA", "Status": "Uploaded"},
    {"Type": "DOC", "Filename": "MO_2024_002.docx", "Size": "850 KB",
     "Metadata": "PROFORMA", "Status": "Uploaded"},
    {"Type": "PDF", "Filename": "PO_2024_003.pdf", "Size": "2.1 MB",
     "Metadata": "PROFORMA", "Status": "Uploaded"},
]

# IW38 order search grid (includes released rows for the print check).
IW38_ORDERS = [
    {"Order": "4001234", "Type": "PM02", "Description": "Pump repair - north unit",
     "Status": "CLSD", "PlannerGroup": "WP-G", "FunLocation": "CLWA-P03-FR",
     "Revision": "R2024-001", "BasicStart": "12.01.2024", "ARBPL": "ME-01"},
    {"Order": "4001235", "Type": "PM01", "Description": "Valve replacement - line 3",
     "Status": "OPEN vrijgegeven CRTD", "PlannerGroup": "Ex-VERM", "FunLocation": "CLWA-P02-BR",
     "Revision": "R2024-002", "BasicStart": "15.03.2024", "ARBPL": "ME-02"},
    {"Order": "4001236", "Type": "PM03", "Description": "Electrical inspection",
     "Status": "vrijgegeven REL PRT", "PlannerGroup": "WP-G", "FunLocation": "CLWA-P01-EL",
     "Revision": "", "BasicStart": "22.06.2024", "ARBPL": "ME-03"},
    {"Order": "4001237", "Type": "PM02", "Description": "Bearing replacement",
     "Status": "vrijgegeven REL", "PlannerGroup": "Ex-VERM", "FunLocation": "CLWA-P03-FR",
     "Revision": "R2024-003", "BasicStart": "02.09.2024", "ARBPL": "ME-01"},
    {"Order": "4001238", "Type": "PM01", "Description": "Piping overhaul - section B",
     "Status": "CRTD", "PlannerGroup": "WP-G", "FunLocation": "CLWA-P04-PI",
     "Revision": "", "BasicStart": "10.10.2024", "ARBPL": "ME-02"},
]

DATASETS = {
    "me5a_pr": ME5A_PR,
    "iw32_attach_read": IW32_ATTACH_READ,
    "iw32_attach_export": IW32_ATTACH_EXPORT,
    "iw38_orders": IW38_ORDERS,
}

DEFAULT_GRID = "me5a_pr"

# --- scalar responses --------------------------------------------------------
# Path-substring -> {member: value}. Mirrors the hardcoded IW32 header/detail
# reads (order number, short text, system status, basic start, work centre).
SCALARS = {
    "STTXT": {"text": "REL VTVB"},
    "CAUFVD-AUFNR": {"text": "4001235"},
    "LTEXT": {"text": "Valve replacement - line 3"},
    "GSTRP": {"text": "28.09.2026"},
    "VAPLZ": {"text": "ME-01"},
}

# --- transactions ------------------------------------------------------------
# Per-tcode grid/export dataset + status bar seed. The fake resolves reads
# from the session's current transaction (tracked from /nXXXX writes) and
# falls back to the defaults.
_NO_DOC = {"text": "No document was created", "type": "E"}
TRANSACTIONS = {
    "ME5A": {"grid": "me5a_pr", "export": "me5a_pr", "sbar": _NO_DOC, "scalars": {}},
    "IW32": {
        "grid": "iw32_attach_read", "export": "iw32_attach_export",
        "sbar": _NO_DOC, "scalars": SCALARS,
    },
    "IW38": {"grid": "iw38_orders", "export": "iw38_orders", "sbar": _NO_DOC, "scalars": {}},
    "ME51N": {
        "grid": "me5a_pr", "export": "me5a_pr",
        "sbar": {"text": "Purchase requisition 10012345 created", "type": "S"}, "scalars": {},
    },
    "IW31": {
        "grid": "me5a_pr", "export": "me5a_pr",
        "sbar": {"text": "Order 10004567 created", "type": "S"},
        # The first-time workaround reads this; a filled value skips the workaround.
        "scalars": {"AUFPAR-PM_AUFART": {"text": "ZA01"}},
    },
    "IW21": {
        "grid": "me5a_pr", "export": "me5a_pr",
        "sbar": {"text": "Notification 10001234 created", "type": "S"}, "scalars": {},
    },
}
