from __future__ import annotations

import os


# CORS Configuration
# ==================
# This local-network-only project uses a whitelist approach with addresses that are
# not sensitive to expose in a public repository:
#
# - 192.168.100.0/24 is a private RFC 1918 range, not publicly routable
# - .local hostnames are mDNS-only, only resolvable within the local network
# - CORS policy is inherently public (revealed in preflight OPTIONS requests)
#
# To customize for different network configurations, override CORS_ALLOWED_ORIGINS
# via environment variable (see ENVIRONMENT.md and docker-compose.yml).
DEFAULT_CORS_ALLOWED_ORIGINS = (
    # Backend host with API port
    "http://192.168.100.99:8000",
    "http://ubuntu26-remote.local:8000",
    "http://drukarka.local:8000",
    # Production app origins served through the frontend reverse proxy
    "http://192.168.100.99",
    "http://192.168.100.99:80",
    "http://ubuntu26-remote.local",
    "http://ubuntu26-remote.local:80",
    # Dev ports (Vite)
    "http://192.168.100.99:5173",
    "http://192.168.100.99:5174",
    "http://192.168.100.99:5175",
    # Ubuntu remote host
    "http://ubuntu26-remote.local:5173",
    "http://ubuntu26-remote.local:5174",
    "http://ubuntu26-remote.local:5175",
    # Printer display host (drukarka.local)
    "http://drukarka.local:5173",
    "http://drukarka.local:5174",
    "http://drukarka.local:5175",
    "http://drukarka.local:80",
    "http://drukarka.local",
    "https://drukarka.local:443",
    "https://drukarka.local",
    # NOTE: localhost/127.0.0.1 origins are intentionally omitted.
    # The session cookie uses SameSite=Lax; on plain HTTP a browser treats
    # localhost:5173 → 192.168.100.99:8000 as cross-site and will not attach
    # the cookie to XHR/fetch requests.  CORS preflight succeeds but every
    # auth-protected endpoint returns 401.  Developers must access the Vite
    # dev server via the LAN IP (e.g. http://192.168.100.99:5173) so that
    # both the frontend and the API share the same registered host and the
    # cookie is sent same-site.  See ENVIRONMENT.md for details.
)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_csv(name: str, default: tuple[str, ...]) -> list[str]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


QUEUE_NAME = os.getenv("QUEUE_NAME", "Canon_MG5350")
PRINTER_IP = os.getenv("PRINTER_IP", "192.168.100.100")
BACKEND_HOST = os.getenv("BACKEND_HOST", "192.168.100.99")
BACKEND_PORT = _env_int("BACKEND_PORT", 8000)
TMP_DIR = os.getenv("TMP_DIR", "/var/tmp/printer-backend")
DB_PATH = os.getenv("DB_PATH", "/var/tmp/printer-backend/app.db")
MAX_UPLOAD_MB = _env_int("MAX_UPLOAD_MB", 50)
PREVIEW_DPI = _env_int("PREVIEW_DPI", 110)
CORS_ALLOWED_ORIGINS = _env_csv("CORS_ALLOWED_ORIGINS", DEFAULT_CORS_ALLOWED_ORIGINS)
SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "local_printer_session")
SESSION_TTL_DAYS = _env_int("SESSION_TTL_DAYS", 30)
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
