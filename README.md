# SAP Work Order Tool

## 2026.11.23.8 — 2026-11-23

- The docked toolbar also re-centres itself if it drifts off its monitor's centre.

## 2026.11.23.7 — 2026-11-23

- The docked toolbar re-fits itself when the open-helper tray changes, instead of keeping an old size until it is hidden and shown again.

## 2026.11.23.6 — 2026-11-23

- Fixed the temporary Dock Diagnostic changing the monitor the toolbar docks on; monitor lookups now use one shared, 64-bit-safe API path.

## 2026.11.23.5 — 2026-11-23

- Added a temporary Dock Diagnostic tool (Settings → Tool windows) to record monitor, DPI and dock geometry while reporting a multi-monitor docking issue.

## 2026.11.23.4 — 2026-11-23

- Docking no longer keeps a stale window size or offset after a helper was open; the bar returns at its packed size, centred on its own monitor.

## 2026.11.23.3 — 2026-11-23

- The collapsed docked toolbar now restores after 200 ms of hover.

## 2026.11.23.2 — 2026-11-23

- The collapsed docked toolbar now restores after 400 ms of hover instead of 1 second.

## 2026.11.23.1 — 2026-11-23

- Editing a main-toolbar button no longer disappears when the docked toolbar auto-hides.
- Tool-window scripts can be customised per PC and survive updates. Settings → Tool windows shows which scripts differ, a diff against the shipped script, and a reset for one script or the whole tool. Local changes are re-applied automatically where possible; a collision is flagged for review.

## 2026.11.22.4 — 2026-11-22

- Settings, the Helpers card and the Update tab follow the chosen tool-window language (Dutch, French, English and Spanish).

## 2026.11.22.3 — 2026-11-22

- The toolbar's week indicator now follows the clock when the app stays open into a new week.

## 2026.11.22.2 — 2026-11-22

- Settings has a new layout: General, Tool windows, Advanced and Update.
- The Tool windows tab shows a screenshot and description for each tool, with one Enable switch and an Open now button.
- Updates now happen on an Update tab inside Settings; the separate update window is gone.
- The SAP Import planner group and SAP system are set in Settings; the Import window no longer duplicates them.
- The main toolbar's Tools and Settings menus are replaced by a Helpers card, opened from a button on both the docked and undocked toolbar.
- Dock top and Auto-hide are no longer in Settings; double-click the logo to dock or undock.

## 2026.11.22.1 — 2026-11-22

- The docked toolbar opens at its normal height after the last helper window is closed while it is hidden.

## 2026.11.21.3 — 2026-11-21

- SAP Import: right-click a row or a selection to upload only those orders to SAP.
- The purchase-request helper keeps working when SAP changes the screen number behind the ATB form.
- Power users can wildcard every MEGUI screen id in the script editor with one click.

## 2026.11.21.2 — 2026-11-21

- The docked toolbar no longer closes the revision dropdown while you choose a value.

## 2026.11.21.1 — 2026-11-21

- The update window shows what an update contains and can restore the version you updated from.
- Choose which tool windows appear from Settings; turning SAP Import off also removes its BCsys counter and status from the toolbar.
- A BCsys outage no longer freezes the toolbar. The indicator says "BCsys unreachable" and retries on click, then resumes the normal check.
- Print Orders stays responsive while it builds PDFs from large photo attachments.
- The docked toolbar keeps its size when a helper window moves to another screen, and waits a second on hover before unfolding.
- Print Orders opens in the right mode: an order already open in IW32 shows its details instead of the list; otherwise the IW38 list opens.
- Process Selected, Retry Failed and Merge PDFs appear only when they apply. A run without errors stitches every processed order automatically; after an error nothing is stitched, and the Auto-merge option is gone.
- Developer toggles (Debug Mode, Debug Console, Fake SAP) are hidden unless Power User is enabled.

## 2026.11.17.1 — 2026-11-17

- Print Orders now reads SAP list exports in Dutch, French, English and Spanish. Columns are recognised by meaning rather than position or heading, so a different column order or a missing optional column no longer breaks the tool.
- When no status column is available, orders are marked Unknown and Print Orders asks before processing them; unrecognised status values are no longer reported as "No Release".
- Tool windows can follow the Windows language, with a manual language choice in Settings.
- The window is called Print Orders again.
- The Scripts menu in Print Orders is hidden unless Power User is enabled.

## 2026.11.02.3 — 2026-11-02

- The Print Orders window is now called Process Orders, which better matches what it does.
- Completed results are kept as one PDF per single order and one merged PDF per batch; the separate work-order and photo parts are deleted after combining.
- Processing a single order again asks before overwriting its PDF and history entry.
- Merge and auto-merge always use every DONE order visible in the table, so a manually processed order is included in the next batch merge.
- A typed or IW32 order that is not in the loaded list joins the table as a DONE row when a list is loaded.
- History shows one clear description per result instead of repeating the order, status and file details.

## 2026.11.02.2 — 2026-11-02

- Print Orders now loads the open IW38 list automatically, keeps a manual Load button, and can print one order, selected rows, or all visible rows.
- Print Orders supports text and SAP user-status filtering (multi-select), retry failed orders, row multi-select, double-click actions and a vertical scrollbar.
- Completed PDFs are kept in a dated output folder with clear file names, and a searchable History pane lists recent runs, opens their PDFs and re-prints an order.
- Print Orders settings (auto-merge, stop on error, maximum attachments, PDF layout) and script editing now live in menus instead of a row of small buttons.

## 2026.11.02.1 — 2026-11-02

- Print Orders can print the single order already open in IW32, without exporting the IW38 grid first.
- Fixed the Print Orders grid squashing its columns instead of scrolling horizontally.

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
