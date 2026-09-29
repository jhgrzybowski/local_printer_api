# Office documents and the next app

## App workflow

1. Read `GET /capabilities` for runtime format availability and limits. Read `GET /options` for the current printer's option values. These routes do not require a session.
2. Sign in or sign up; send the `local_printer_session` cookie on protected requests.
3. Send multipart `file` to `POST /files`. Native PDF, PNG, JPEG, and UTF-8 text remain supported. Office-enabled deployments additionally accept **DOCX, XLSX, PPTX, ODT, ODS, ODP**. Filename extension and archive content must agree; HTTP Content-Type is not trusted.
4. An Office upload succeeds only after conversion. Retain `file_id`. The response includes original MIME/size, converted PDF page count, `converted`, `printable_mime`, `printable_sha256`, warnings, and `pdf_url`. `GET /files/{file_id}` retrieves the same metadata later. Legacy uploads can have a null hash.
5. Display warnings and review `GET /files/{file_id}/preview` plus individual PNG pages, or download the authenticated `pdf_url`. PDF pages render on demand. Preview and printing use the same stored PDF; there is no conversion at print time. PNG previews remain an approximation of physical output and do not simulate duplex, paper, cropping, or color management.
6. Call `POST /print/validate` with the same body intended for `/print`. This selects pages and maps options without spooling or creating history. `valid` means every option is supported; `ready_for_print` reports the separate current queue state. Check both. Printing rechecks capabilities and readiness.
7. Submit `POST /print`, then follow `/jobs/{job_id}` and `/history`. CUPS completion means the spooler finished; it is not independent evidence of paper quality.

Example request, usable with both validation and submission:

```json
{"file_id":"returned-file-id","strict_options":true,"options":{"copies":1,"pages":"1-2","paper_size":"A4","orientation":"portrait","color_mode":"monochrome","duplex":"none","quality":"normal"}}
```

**Compatibility change in API 0.2:** unsupported options now produce 422 before a job is submitted. `strict_options: false` explicitly restores dropping unsupported options with warnings. Unknown top-level request keys are rejected. Unspecified print options still use current CUPS defaults; apps needing repeatable output should supply explicit options. Validation is a snapshot, not a reservation. Print requests are not yet idempotent: do not automatically retry a timed-out POST; inspect history first.

## Rendering policy and limits

Office documents are converted once using LibreOffice, with a private profile and stable locale/timezone. The generated PDF is retained for repeat preview/print requests, with its SHA-256. Reuploading the same Office file is not guaranteed to produce identical bytes: document fields, spreadsheet recalculation, font availability, and renderer upgrades can change output.

Spreadsheets follow their saved print areas, page styles, and scaling. We do not force each sheet onto one page: LibreOffice's `SinglePageSheets` mode ignores print ranges and also includes hidden sheets. Verify the generated PDF before printing. Sheet selection, range selection, and layout overrides are future extensions. Formula fidelity and fonts are not guaranteed to match Microsoft Office.

Default conversion limits: 90 seconds, 1536 MiB virtual address space, 100 MiB per output file, 500 resulting PDF pages, 2000 archive entries, 100 MiB total expanded archive bytes, and 4 MiB per XML or relationship part. One conversion runs at a time per shared TMP_DIR, including across API workers; simultaneous Office uploads receive 503 and can retry. Native uploads remain available. Conversion work runs outside the event loop.

The converter disables macros, rejects encrypted/macro/embedded-object/external-data packages, and denies network socket creation through seccomp. These are defense measures, not a complete filesystem sandbox. Keep uploads within the trusted LAN deployment boundary and keep LibreOffice patched. Old binary DOC/XLS/PPT, macro-enabled formats, encrypted files, CSV layout import, and arbitrary archives are not offered as Office conversions; save them as DOCX/XLSX/PPTX or PDF first.

Errors retain the existing `detail` response convention: 415 unsupported/unsafe Office container, 422 conversion failure, 413 size/page limit, 503 converter absent/disabled/busy, 504 timeout. No file ID or printable record is published after failed conversion. Successful converted files share the original upload's ownership, lease, and retention lifecycle.

## Deployment

The Docker image installs LibreOffice Writer/Calc/Impress only for on-demand subprocess conversion, plus Liberation/Carlito/Caladea fonts and libseccomp2. It runs no Office daemon. `INSTALL_OFFICE=false` disables these build dependencies; `/capabilities` then reports conversion unavailable. `OFFICE_ENABLED=false` disables conversion at runtime. Limits are configurable with `OFFICE_TIMEOUT_SECONDS`, `OFFICE_MEMORY_MB`, and `OFFICE_MAX_PAGES`. Office conversion requires Linux and libseccomp.

The existing host stack is not automatically replaced by this change. Validate an isolated image before deployment. All generated PDFs and temporary conversion work live below TMP_DIR on persistent deployment storage.

## Printer discovery and suggested extensions

Read-only discovery on the Canon MG5350 queue advertised duplex (both edges), Gray/Black/RGB color modes, A4/A5/A6 and photo sizes, rear/cassette/CD sources, plain/photo/envelope media, borderless mode, and Gutenprint scaling/color controls. These are driver-advertised capabilities, not proof that every media/duplex/borderless combination is physically valid.

Priorities after Office support:

- Idempotent print submission with persisted request keys and explicit uncertain-submission recovery, to prevent duplicate pages after network retries.
- Spreadsheet worksheet/range/layout selection with a newly generated PDF preview for each selection.
- Paper-source selection and tested photo presets combining media, size, borderless, and quality. Validate combinations against PPD constraints before offering them.
- Multi-document batches with per-file outcome reporting and a clear partial-failure policy.
- Multi-page TIFF normalization and per-user storage quotas.
- Scanning as a separately probed capability, not inferred from CUPS printing support. Ink reporting remains best-effort.

References: [LibreOffice command-line conversion](https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html), [PDF export options and spreadsheet policy](https://help.libreoffice.org/latest/en-US/text/shared/guide/pdf_params.html).

## Reproduce the isolated tests

The tests do not mount the host CUPS socket and do not submit physical jobs.
They use real LibreOffice and Poppler for conversion and preview, and a fake CUPS
client for API submission/ownership checks.

```sh
docker build -t local-printer-api:office-test .
docker build -f Dockerfile.tests -t local-printer-api:office-tests .
docker run --rm --no-healthcheck --network none --memory 3g --cpus 2 \
  --mount type=bind,src="$PWD",dst=/workspace,readonly \
  -e RUN_OFFICE_INTEGRATION=1 -e PYTHONDONTWRITEBYTECODE=1 \
  --entrypoint python local-printer-api:office-tests \
  -m pytest -q -p no:cacheprovider
```

Live validation against a deployed backend (creates a test account and two
uploads, logs out afterward, submits no jobs by default):

```sh
.venv/bin/python scripts/smoke_office_api.py
```

The optional `--print` flag submits one monochrome A4 page per format and waits
for completion. Only use it when physical printing is authorized.
