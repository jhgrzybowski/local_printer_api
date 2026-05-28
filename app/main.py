from contextlib import asynccontextmanager
import logging
from pathlib import Path
from typing import Any

from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from app.models.print_options import PrintRequest
from app.services.cups_client import JOB_SCOPE_TO_CUPS, CupsClient, CupsClientError
from app.services.auth import AuthError, AuthService, LoginSession, public_user
from app.services.database import Database, User
from app.services.file_storage import StoredFile, StorageError, TempFileStorage
from app.services.options_summary import build_options_summary
from app.services.preview import PreviewError, PreviewService
from app.services.print_service import PrintRequestError, submit_print_job
from app.services.status_translator import translate_error_status, translate_queue_status
from app.settings import (
    CORS_ALLOWED_ORIGINS,
    DB_PATH,
    QUEUE_NAME,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_SECURE,
    SESSION_TTL_DAYS,
)


OPENAPI_YAML_PATH = Path(__file__).resolve().parent.parent / "openapi.yaml"
LOGGER = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_database().delete_expired_sessions()
    yield


app = FastAPI(title="Local Printer API", docs_url=None, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Accept"],
)


class SignupRequest(BaseModel):
    username: str
    password: str
    display_name: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


@app.get("/openapi.yaml", include_in_schema=False)
def openapi_yaml() -> FileResponse:
    if not OPENAPI_YAML_PATH.exists():
        raise HTTPException(status_code=404, detail="openapi.yaml not found")
    return FileResponse(OPENAPI_YAML_PATH, media_type="application/yaml")


@app.get("/docs", include_in_schema=False)
def swagger_docs() -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url="/openapi.yaml",
        title="Local Printer API Docs",
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "local-printer-api"}


def get_cups_client() -> CupsClient:
    return CupsClient()


def get_file_storage() -> TempFileStorage:
    return TempFileStorage()


def get_database() -> Database:
    database = getattr(app.state, "database", None)
    if database is None:
        database = Database(DB_PATH)
        app.state.database = database
    return database


def get_auth_service(database: Database = Depends(get_database)) -> AuthService:
    return AuthService(database)


def require_current_user(
    request: Request,
    auth: AuthService = Depends(get_auth_service),
) -> User:
    user = auth.user_for_token(request.cookies.get(SESSION_COOKIE_NAME))
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


@app.post("/auth/signup")
def signup(
    payload: SignupRequest,
    request: Request,
    response: Response,
    auth: AuthService = Depends(get_auth_service),
) -> dict[str, object]:
    try:
        session = auth.signup(
            username=payload.username,
            password=payload.password,
            display_name=payload.display_name,
            user_agent=request.headers.get("user-agent"),
            ip_address=client_ip(request),
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    set_session_cookie(response, session)
    return auth_response(session)


@app.post("/auth/login")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    auth: AuthService = Depends(get_auth_service),
) -> dict[str, object]:
    try:
        session = auth.login(
            username=payload.username,
            password=payload.password,
            user_agent=request.headers.get("user-agent"),
            ip_address=client_ip(request),
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    set_session_cookie(response, session)
    return auth_response(session)


@app.post("/auth/logout")
def logout(
    request: Request,
    response: Response,
    auth: AuthService = Depends(get_auth_service),
) -> dict[str, bool]:
    auth.logout(request.cookies.get(SESSION_COOKIE_NAME))
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"logged_out": True}


@app.get("/auth/me")
def me(current_user: User = Depends(require_current_user)) -> dict[str, object]:
    return {"user": public_user(current_user)}


@app.get("/status")
def status(client: CupsClient = Depends(get_cups_client)) -> dict[str, object]:
    try:
        payload = translate_queue_status(client.get_queue())
        payload["cups"] = {"available": True, "error": None}
        return payload
    except CupsClientError as exc:
        payload = translate_error_status(QUEUE_NAME, str(exc))
        payload["cups"] = {"available": False, "error": str(exc)}
        return payload


@app.post("/print")
def print_file(
    request: PrintRequest,
    current_user: User = Depends(require_current_user),
    client: CupsClient = Depends(get_cups_client),
    storage: TempFileStorage = Depends(get_file_storage),
    database: Database = Depends(get_database),
) -> dict[str, object]:
    record = get_user_file_record(storage, request.file_id, current_user)

    try:
        result = submit_print_job(client, storage, record, request.options)
    except PrintRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    try:
        history_id = database.insert_print_history(
            user_id=current_user.id,
            file_id=record.file_id,
            original_filename=record.original_filename,
            detected_mime=record.detected_mime,
            size_bytes=record.size_bytes,
            page_count=record.page_count,
            requested_options=request.options.model_dump(),
            applied_options=result["applied_options"],
            cups_job_id=int(result["job_id"]),
            warnings=[str(warning) for warning in result["warnings"]],
        )
    except Exception:
        LOGGER.exception(
            "Failed to persist print history after submitting CUPS job %s",
            result["job_id"],
        )
        history_id = None
        result["warnings"] = [
            *[str(warning) for warning in result["warnings"]],
            "Print history could not be persisted; CUPS job was submitted",
        ]
    result["history_id"] = history_id
    return result


@app.get("/jobs")
def list_jobs(
    scope: str = Query("active", description="Job scope: active, completed, or all"),
    current_user: User = Depends(require_current_user),
    client: CupsClient = Depends(get_cups_client),
) -> dict[str, object]:
    _ = current_user
    if scope not in JOB_SCOPE_TO_CUPS:
        raise HTTPException(status_code=400, detail="Invalid job scope")
    try:
        return {
            "scope": scope,
            "queue": client.queue_name,
            "jobs": client.list_jobs(scope),
            "counts": client.job_counts(),
        }
    except CupsClientError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/options")
def get_options(
    debug: bool = False,
    client: CupsClient = Depends(get_cups_client),
) -> dict[str, object]:
    try:
        capabilities = client.get_option_capabilities()
    except CupsClientError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return build_options_summary(client.queue_name, capabilities, include_debug=debug)


@app.get("/jobs/{job_id}")
def get_job(
    job_id: int,
    current_user: User = Depends(require_current_user),
    client: CupsClient = Depends(get_cups_client),
) -> dict[str, object]:
    _ = current_user
    try:
        job = client.get_job(job_id)
    except CupsClientError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.delete("/jobs/{job_id}")
def cancel_job(
    job_id: int,
    current_user: User = Depends(require_current_user),
    client: CupsClient = Depends(get_cups_client),
    database: Database = Depends(get_database),
) -> dict[str, object]:
    require_user_cups_job(database, current_user, job_id)
    try:
        return client.cancel_job(job_id)
    except CupsClientError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/jobs/{job_id}/forget")
def forget_job(
    job_id: int,
    current_user: User = Depends(require_current_user),
    client: CupsClient = Depends(get_cups_client),
    database: Database = Depends(get_database),
) -> dict[str, object]:
    require_user_cups_job(database, current_user, job_id)
    try:
        result = client.forget_job(job_id)
    except CupsClientError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if result.get("forgotten") is False:
        raise HTTPException(status_code=409, detail=result)
    return result


@app.post("/files")
async def upload_file(
    file: UploadFile = File(...),
    current_user: User = Depends(require_current_user),
    storage: TempFileStorage = Depends(get_file_storage),
) -> dict[str, object]:
    try:
        record = await storage.save_upload(file, owner_user_id=current_user.id)
    except StorageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return file_response(record)


@app.get("/files/{file_id}/preview")
def list_previews(
    file_id: str,
    current_user: User = Depends(require_current_user),
    storage: TempFileStorage = Depends(get_file_storage),
) -> dict[str, object]:
    record = get_user_file_record(storage, file_id, current_user)

    preview_service = PreviewService(storage)
    try:
        paths = preview_service.ensure_previews(record)
    except PreviewError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return {
        "file_id": record.file_id,
        "page_count": record.page_count,
        "pages": [
            {
                "page": index,
                "url": f"/files/{record.file_id}/preview/{index}",
                "size_bytes": path.stat().st_size,
            }
            for index, path in enumerate(paths, start=1)
        ],
    }


@app.get("/files/{file_id}/preview/{page}")
def get_preview_page(
    file_id: str,
    page: str,
    current_user: User = Depends(require_current_user),
    storage: TempFileStorage = Depends(get_file_storage),
) -> FileResponse:
    record = get_user_file_record(storage, file_id, current_user)

    preview_service = PreviewService(storage)
    try:
        page_number = parse_page_number(page)
        path = preview_service.preview_path(record, page_number)
    except PreviewError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return FileResponse(path, media_type="image/png")


@app.get("/me/preferences")
def get_preferences(
    current_user: User = Depends(require_current_user),
    database: Database = Depends(get_database),
) -> dict[str, object]:
    return {"preferences": database.get_preferences(current_user.id) or {}}


@app.put("/me/preferences")
def put_preferences(
    preferences: dict[str, Any] = Body(...),
    current_user: User = Depends(require_current_user),
    database: Database = Depends(get_database),
) -> dict[str, object]:
    return {"preferences": database.upsert_preferences(current_user.id, preferences)}


@app.get("/history")
def list_history(
    current_user: User = Depends(require_current_user),
    database: Database = Depends(get_database),
) -> dict[str, object]:
    return {"history": database.list_print_history(current_user.id)}


@app.get("/history/{history_id}")
def get_history_entry(
    history_id: int,
    current_user: User = Depends(require_current_user),
    database: Database = Depends(get_database),
) -> dict[str, object]:
    entry = database.get_print_history(current_user.id, history_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="History entry not found")
    return entry


def file_response(record: StoredFile) -> dict[str, object]:
    return {
        "file_id": record.file_id,
        "original_filename": record.original_filename,
        "detected_mime": record.detected_mime,
        "size_bytes": record.size_bytes,
        "page_count": record.page_count,
        "preview_available": record.preview_available,
    }


def get_user_file_record(
    storage: TempFileStorage,
    file_id: str,
    current_user: User,
) -> StoredFile:
    record = storage.get_record(file_id)
    if record is None or record.owner_user_id != current_user.id:
        raise HTTPException(status_code=404, detail="File not found")
    return record


def require_user_cups_job(database: Database, current_user: User, job_id: int) -> None:
    if not database.user_has_cups_job(current_user.id, job_id):
        raise HTTPException(status_code=404, detail="Job not found")


def parse_page_number(page: str) -> int:
    try:
        page_number = int(page)
    except ValueError as exc:
        raise PreviewError("Invalid page number", 400) from exc
    if page_number < 1:
        raise PreviewError("Invalid page number", 400)
    return page_number


def set_session_cookie(response: Response, session: LoginSession) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session.token,
        max_age=SESSION_TTL_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def auth_response(session: LoginSession) -> dict[str, object]:
    return {
        "user": public_user(session.user),
        "session": {"expires_at": session.expires_at},
    }


def client_ip(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host
