# Known Limitations and Future Steps

This document tracks intentional v1 limitations and possible future improvements.

---

## Current limitations

## 1. Backend only

The current project is backend-only. The API already supports local user accounts,
cookie sessions, per-user preferences, and SQLite print submission history.

Not included in v1:

- React frontend
- admin UI
- cloud sync

The backend is intended to expose a clean REST API for a future LAN frontend.

---

## 2. LAN-only authentication

Signup creates a local account; signup and login set an HttpOnly
`local_printer_session` cookie. Upload, preview, print, job, preference, and
history endpoints require a valid session. Files, preferences, history, and API
job views are scoped to the signed-in user. Health, printer status, options,
signup, and login are publicly reachable on the bound network.

Security limits:

* Anyone who can reach the API can create an account; there is no signup approval
  or administrator role.
* The default plain-HTTP deployment does not encrypt credentials or cookies in
  transit. Use only on a trusted LAN.
* Authentication does not make the API safe to expose to the internet.

Current recommendation:

```text
Bind only to LAN.
Do not expose to the internet.
```

For session setup and cookie checks, see `README.md` and `TROUBLESHOOTING.md`.

---

## 3. CUPS is the source of truth

The backend intentionally delegates real print state to CUPS.

Implication:

* SQLite stores per-user print submission records, requested/applied options,
  the CUPS job identity, and the last known job status. It does not replace
  live CUPS state. Reading `/history` refreshes nonterminal statuses from
  matching CUPS jobs; cancel and forget actions also update stored status.
  If CUPS is unavailable, history keeps the last known status.
* Completed historical jobs may remain visible or disappear depending on CUPS
  configuration.
* Historical completed/canceled/aborted jobs are not active queue work and are
  not treated as cancelable tasks by the API.
* Job status accuracy depends on CUPS and printer reporting.

Future options:

* reprint support,
* an audit log beyond the existing per-user submission history.

---

## 4. Preview is approximate

PDF/image preview is generated before CUPS/Gutenprint final processing.

Preview may differ from physical print because of:

* driver margins,
* scaling,
* printable area,
* media type,
* color handling,
* duplex layout,
* Gutenprint-specific transformations.

Current wording:

```text
Preview is for user convenience and is not guaranteed to match final printer-driver output exactly.
```

Future options:

* expose printable area,
* warn when document page size differs from selected paper,
* generate preview with selected paper size overlay,
* add margin visualization,
* add better fit-to-page simulation.

---

## 5. Office documents are not included yet

Current upload scope:

* PDF
* PNG
* JPEG
* plain text

Not yet supported:

* DOCX
* ODT
* XLSX
* PPTX

Reason:

* Office conversion requires LibreOffice.
* LibreOffice can be slow on low-end servers.
* Conversion adds more failure modes.

Future option:

* add optional `libreoffice --headless --convert-to pdf`,
* wrap conversion in timeout,
* document dependency separately,
* keep disabled unless system package is installed.

---

## 6. Ink reporting may be incomplete

CUPS marker attributes may not expose complete ink levels for this printer/driver combination.

Potential attributes:

* `marker-names`
* `marker-levels`
* `marker-colors`
* `marker-types`

Limitations:

* older printer firmware may not expose everything,
* LPD transport may expose less status than richer protocols,
* Gutenprint/CUPS marker support may vary.

Future options:

* scrape Canon HTTP status page on port 80,
* parse ink/status information from printer web UI,
* expose best-effort cartridge state,
* clearly mark unknown values as unknown.

---

## 7. Printer is not handled as IPP Everywhere

The Canon PIXMA MG5350 is handled through:

```text
Gutenprint + LPD
```

Verified setup:

```text
lpd://192.168.100.100/PASSTHRU
gutenprint.5.3://bjc-PIXMA-MG5350/expert
```

Driverless IPP failed in this environment.

Implication:

* API option mapping must be based on actual CUPS/PPD options.
* Do not assume IPP Everywhere keys such as `print-color-mode` or `media` will work directly.
* Gutenprint-specific options such as `ColorModel`, `Resolution`, `Duplex`, `PageSize`, `MediaType`, `StpiShrinkOutput`, and `StpOrientation` may be relevant.

Future option:

* make queue capability detection more generic,
* support multiple printer profiles,
* support IPP Everywhere printers separately.

---

## 8. Option mapping is conservative

Current print API uses frontend-style options, then maps them to detected CUPS/Gutenprint capabilities.

Unsupported options are dropped and reported.

Known behavior:

* `collate` is ignored quietly when `copies=1`.
* `collate` may warn for multiple copies if no compatible CUPS option is detected.
* `media_type` maps only when safe detected values exist.
* `fit_to_page` maps only when a detected scaling option exists.
* orientation may use `StpOrientation` or standard `orientation-requested`.

Future improvements:

* stronger `/options` contract for frontend,
* option grouping by feature,
* exact mapping table from detected PPD,
* UI hints for unsupported features,
* per-queue mapping profiles.

---

## 9. Duplex needs real-world validation

The printer supports duplex, and the backend maps duplex through detected CUPS/Gutenprint options.

Still worth validating manually:

* `duplex: "none"`
* `duplex: "long-edge"`
* `duplex: "short-edge"`

Future test cases:

* portrait long-edge,
* landscape long-edge,
* portrait short-edge,
* landscape short-edge,
* multi-page PDF duplex behavior.

---

## 10. Landscape orientation needs validation

Portrait printing has been validated in the basic print path.

Landscape should be tested with:

* landscape PDF,
* portrait PDF printed as landscape,
* page-range filtered landscape PDF,
* image file landscape print.

Future improvement:

* add generated landscape smoke-test PDF,
* document physical result expectations.

---

## 11. Fit-to-page behavior may vary

`fit_to_page` is only safe when the queue exposes a compatible scaling/shrink option.

In Gutenprint, relevant options may include:

```text
StpiShrinkOutput
```

Limitations:

* not every document size maps predictably,
* image scaling and PDF scaling may differ,
* CUPS/Gutenprint may handle margins differently.

Future improvements:

* expose fit/scaling support clearly in `/options`,
* add generated non-A4 test PDF,
* compare output with and without fit-to-page.

---

## 12. No SNMP support

SNMP was not available in the observed printer probing.

Current status:

```text
SNMP is not used.
```

Future options:

* only add SNMP if port 161 is available and useful,
* use `pysnmp` optionally,
* keep it disabled by default.

---

## 13. No Wake-on-LAN

Wake-on-LAN is not implemented.

Reason:

* Canon MG5350 Wi-Fi wake behavior is not reliable/documented enough for this setup.
* Printer may be off, sleeping, or disconnected from Wi-Fi.

Current behavior:

* report offline/unreachable clearly,
* ask user to power on printer.

Future option:

* only revisit if a reliable wake method is found.

---

## 14. Temporary storage is local and simple

Uploads and previews are stored under `TMP_DIR`.

Current limitations:

* uploads, metadata, previews, and filtered PDFs are local temporary files;
  they are not retained as durable documents,
* uploaded files have owner IDs and protected routes check ownership,
* there is no storage quota beyond the per-upload size limit,
* hourly in-process maintenance removes uploads, metadata, previews, and
  filtered PDFs after `UPLOAD_TTL_DAYS` (7 by default), and print history
  after `HISTORY_TTL_DAYS` (90 by default),
* active CUPS jobs and files being rendered or spooled are protected from
  cleanup. If CUPS cannot be queried, file and history cleanup is deferred.

SQLite user, session, preference, and print history records are separate from
these temporary files. Configure `DB_PATH` on persistent storage for deployment.
`GET /history` is paginated (`limit` 1–100, default 50; `offset` starts at 0).
When an expired history record is removed, its API job ownership claim is also
lost, even if CUPS still retains the job.

Future improvements:

* max total storage size,
* per-user storage quota,
* manual cleanup endpoint for admin use.

---

## 15. No multi-printer support yet

The backend is currently centered around one configured queue.

Future improvements:

* list all CUPS printers,
* support multiple queues,
* per-printer `/options`,
* per-printer status,
* default queue selection,
* printer profiles.

---

## Future roadmap

## Near-term

Recommended next steps:

1. Validate duplex:

   * long-edge
   * short-edge

2. Validate landscape:

   * generated landscape PDF
   * image print

3. Validate color:

   * color PDF
   * color image

4. Validate media type mapping:

   * plain
   * photo/glossy if available

5. Improve `/options` for frontend:

   * stable response shape,
   * clear unsupported indicators,
   * raw debug mode remains optional.

---

## Medium-term

Potential improvements:

* React frontend.
* Drag-and-drop upload.
* Preview pane.
* Print options sidebar.
* Status banner.
* Job list with cancel button.
* Better error UX.

---

## Longer-term

Possible future directions:

* Reprint previous job.
* Named print presets beyond the current saved per-user preferences.
* WebSocket or Server-Sent Events for live job/status updates.
* Canon HTTP status scraping for ink/errors.
* Multi-printer support.
* Optional Office document conversion.
* Nginx reverse proxy or systemd service deployment.
* Better frontend accessibility.
