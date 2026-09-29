# Office pipeline validation — 2026-09-28–29

## Scope and implementation

Branch: `codex/office-document-pipeline`, based on merged master `e733e5b`.
The existing frontend was not examined. Host packages and printer configuration
were not changed. Docker images and isolated test containers were created with
explicit approval. The main backend was then deployed with explicit approval
to deploy without printing; its existing database and upload volumes were retained.

Implemented six Office formats (DOCX/XLSX/PPTX/ODT/ODS/ODP), content validation,
bounded LibreOffice conversion, a persisted PDF shared by preview and printing,
file metadata/PDF endpoints, format discovery, and print preflight. Unsupported
options are rejected by default. Upload processing also rejects corrupt images
and zero-page PDFs. Preview rendering has a time limit and bounded pixel size.

## Printer discovery

The live `Canon_MG5350` queue uses CUPS+Gutenprint 5.3.4 and
`lpd://192.168.100.100/PASSTHRU`. Read-only checks reported idle, enabled,
accepting jobs, network reachable on port 515, and ready for printing.

CUPS advertises PDF, PostScript, PNG, JPEG, TIFF, and text among its accepted
spool formats. Its Office support still requires conversion before submission.
The driver advertises both duplex binding edges, grayscale/color, photo/envelope
media, many paper sizes, rear/cassette/CD sources, and borderless/scaling controls.
These advertisements do not validate every combination on physical media.

## Verification

Deployed image: `sha256:b8fca18734e310d6ef0ec98237bfe4df7009d80fa8b3cce29bcaeb5162e928a7`.

- Existing host-environment suite: 164 passed before adding the Office tests.
- First isolated Office run: 40 passed, including real conversions and PNG rendering for all six formats.
- Full isolated suite: 207 passed; one upstream Starlette TestClient/httpx deprecation warning.
- Final affected tests after the last validation guards: 92 passed; the same upstream test-client deprecation warning.
- Python compilation, Compose configuration validation, and `git diff --check` passed.

Real conversion tests use LibreOffice 7.4.7.2 and Poppler in the candidate's
Debian image. Test containers have no network, no host CUPS socket, and a read-only
repository mount. The suite checks format detection, corrupt/unsafe input,
wrong extensions, archive limits, converter missing/busy/timeout/failure states,
process restrictions, cleanup, retention leases, ownership, PDF hashes,
page selection, strict options, preview behavior, and API specification fields.

Dedicated integration tests exercise actual DOCX and XLSX uploads through the
API, PDF download, rendered PNG, preflight, and a mocked CUPS submission. A real
XLSX conversion excludes a hidden sheet and cells outside its saved print area.

## Live deployment verification — September 29

The main backend is running the image above, healthy, bound to
`192.168.100.99:8000`, with both original persistent volumes. `/capabilities`
reports all six Office formats available. The application source and OpenAPI
inside this image were compared with the repository and matched.

The live smoke script completed without `--print`: DOCX and XLSX upload,
conversion, PDF text/page/hash validation, PNG preview, and `/print/validate`
all passed. Both generated PDFs have one page. Repeated downloads matched
byte-for-byte. Real CUPS option mapping returned copies=1, Duplex=None,
StpOrientation=Portrait, PageSize=A4, ColorModel=Gray, and Resolution=600dpi.

No print jobs were submitted: CUPS job IDs were `[21]` both before and after.
Physical Office printing was deliberately omitted at the user's instruction.
The smoke session logged out successfully. Two generated test uploads remain
under the normal retention policy for user `office_smoke_4d2a7b06af`.

The previous image is still available for rollback:
`sha256:a4d1a012c3f3dbdbe421842ddf5f84956843aaf38e959e6d617f0e6599cb0022`.
No queue reconfiguration or host package installation was performed.

## Readiness assessment and remaining limits

This is a usable document pipeline for the upcoming LAN app. The app can discover
available formats, upload once, review the exact stored PDF, choose supported
options, preflight, submit, and follow job/history endpoints without doing its
own Office conversion.

“Deterministic” here means repeat operations on a given file ID use the same PDF
bytes and selected PDF pages. It does not promise Microsoft Office layout parity,
identical re-conversion across LibreOffice/font versions, or a physical result
independent of printer media and driver defaults. Explicit print options and PDF
review remain required for predictable output.

Print POSTs are not idempotent; after an ambiguous network failure the app must
inspect history instead of automatically resubmitting. Persisted idempotency keys
and an uncertain-submission recovery flow are the highest-priority next addition.
Other useful extensions are spreadsheet sheet/range/layout selection, validated
photo/media presets and paper-source selection, multi-page TIFF normalization,
batch submissions, storage quotas, and separately verified scanning support.

Old binary DOC/XLS/PPT, macro-enabled/encrypted documents, and embedded/external
active content are not supported. LibreOffice conversions run one at a time per
shared storage root; a concurrent request gets a documented 503. The converter's
process limits and network restrictions are not a complete filesystem sandbox.
The deployment remains intended for trusted LAN clients.

See [Office and app integration](OFFICE_AND_APP_INTEGRATION.md) for endpoint
contracts, migration notes, limits, and reproducible test commands.
