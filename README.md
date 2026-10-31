# SAP Work Order Tool

## 2026.10.31.5 — 2026-10-31

- SAP Import: cancelled, failed and pending rows now follow the same rules everywhere, so status chips, colours and counters can no longer disagree after a run.

## 2026.10.31.4 — 2026-10-31

- Docked toolbar now has a Helpers rail for Purchase Request, Print Orders, SAP Import and Revisions; its gear opens Settings. The docked menu bar is gone, so dropdowns no longer fight the docked window.
- Fixed docked auto-hide getting stuck while the order lookup was open, and week-revision dialogs disappearing when the toolbar auto-hid.
- Opening an ATB, work order, or created document in SAP now brings the SAP GUI to the front.
- A newly created ATB now appears in the ATB overview grid immediately.
- Upload metadata now asks for a document type (PROFORMA prefilled); selecting an ATB prefills the description with its vendor.
- Plant, storage location, purchasing group, purchasing org, material group and G/L account moved into a collapsed Advanced settings panel; it turns red and stays open while one is empty.
- Power User setting enables right-click editing of tool-window buttons; all end-user edits to defaults, labels and column layouts now survive updates.
- History is grouped by latest activity, supports searching every field, and has a clear-search button and filtered count.
- Late uploads move the order into the latest day; old orders are no longer dropped from history.
- Copying a work order includes its whole ATB/upload tree; descriptions are capped at 50 characters, supplier is shown per ATB, and groups have visual dividers with Created/Updated/Uploaded dates.
- SAP Import now uses its own indigo accent for its title bar and primary buttons.

## 2026.10.31.3 — 2026-10-31

- Main toolbar buttons can be customised per user: right-click a button to change its label or SAP script, and your changes are kept across updates.
- Updates are checked and applied from Settings; your settings and toolbar scripts are preserved.
- No console windows flash while checking for or applying an update.
