## Project
Python 3 FastAPI backend for Canon PIXMA MG5350 printer manager on LAN.

## Constraints
- Target host: Ubuntu 26.04 server, low-end machine: i3 6th gen, 8 GB RAM, HDD.
- Prefer simple, synchronous, debuggable code.
- Avoid heavy background services unless necessary.
- SQLite stores local users, sessions, preferences, and print submission history.
- Protected API routes require the `local_printer_session` HttpOnly cookie. Signup is open to LAN clients; authentication does not replace LAN-only binding.
- Do not expose the service to the internet.
- Do not install random dependencies without explaining why.

## Architecture
- FastAPI REST API.
- CUPS is the source of truth for printer queues/jobs.
- pycups wrapper in app/services/cups_client.py.
- Printer queue name: Canon_MG5350.
- Printer IP: 192.168.100.100.
- Backend host: 192.168.100.99.
- Temporary files under /var/tmp/printer-backend or configurable TMP_DIR.
- SQLite path is configurable with DB_PATH; keep it on persistent storage for deployment.
- Upload metadata records an owner user ID; files, history, preferences, and API job views are scoped to the signed-in user.
- In-process maintenance removes expired uploads and print history (default 7 and 90 days); active jobs and CUPS outages defer cleanup.

## Current scope and priorities
- Probe script and CUPS queue setup, health/status, PDF/image/text upload, preview, print submission, PDF page ranges, job list/cancel, and CUPS/PPD option mapping are implemented.
- Keep CUPS authoritative for live queue and job state; SQLite history records submissions for each user.
- Monitor retention and disk use on the target host; prioritize accurate option discovery/mapping and physical print validation.
- Optional Office conversion comes later.

## Important trade-offs
- Preview is an approximation, not a guaranteed exact rendering of the final CUPS output.
- Ink reporting may not work reliably via CUPS; treat HTTP scraping as second-pass.
- LibreOffice conversion is optional and must be isolated behind a timeout.
- Do not implement Wake-on-LAN for MG5350.
- Do not use SNMP unless port 161 is actually available.

## Testing
- Unit tests for page ranges, option mapping, translator.
- Mock CUPS for API tests.
- Integration tests requiring real printer should be explicitly marked.
