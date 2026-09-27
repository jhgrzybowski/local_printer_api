import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

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
from starlette.background import BackgroundTask
from pydantic import BaseModel

from app.models.print_options import PrintRequest
from app.services.cups_client import JOB_SCOPE_TO_CUPS, CupsClient, CupsClientError
from app.services.auth import AuthError, AuthService, LoginSession, public_user
from app.services.database import Database, JobClaim, User
from app.services.file_storage import StoredFile, StorageError, TempFileStorage
from app.services.maintenance import INTERVAL_SECONDS, maybe_run_maintenance
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
HISTORY_CUPS_STATES = frozenset({
    "pending", "pending-held", "processing", "processing-stopped",
    "canceled", "aborted", "completed",
})
HISTORY_TERMINAL_STATES = frozenset({"canceled", "aborted", "completed", "forgotten"})


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_database().delete_expired_sessions()
    task = asyncio.create_task(periodic_maintenance())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def periodic_maintenance() -> None:
    while True:
        await asyncio.sleep(INTERVAL_SECONDS)
        await asyncio.to_thread(maybe_run_maintenance, get_database(), get_file_storage(), get_cups_client())


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
        with storage.lease(record.file_id):
            result = submit_print_job(client, storage, record, request.options)
    except (PrintRequestError, StorageError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    submitted_job = None
    try:
        submitted_job = client.get_job(int(result["job_id"]))
        job_claim = claim_for_job(current_user.id, submitted_job, client.queue_name)
    except CupsClientError:
        LOGGER.exception("Could not read submitted CUPS job %s", result["job_id"])
        job_claim = None
    if job_claim is None:
        result["warnings"] = [
            *[str(warning) for warning in result["warnings"]],
            "CUPS job identity could not be verified; job management is unavailable",
        ]
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
            status=(history_cups_state(submitted_job) if job_claim else None) or "submitted",
            job_claim=job_claim,
        )
    except Exception:
        LOGGER.exception(
            "Failed to persist print history after submitting CUPS job %s",
            result["job_id"],
        )
        fallback_saved = False
        if job_claim is not None:
            try:
                database.save_fallback_job_claim(job_claim)
                fallback_saved = True
            except Exception:
                LOGGER.exception("Could not save fallback ownership for CUPS job %s", result["job_id"])
        history_id = None
        result["warnings"] = [
            *[str(warning) for warning in result["warnings"]],
            "Print history could not be persisted; CUPS job was submitted",
        ]
        if not fallback_saved:
            result["warnings"].append("Job ownership could not be saved; job management is unavailable")
    result["history_id"] = history_id
    return result


@app.get("/jobs")
def list_jobs(
    scope: str = Query("active", description="Job scope: active, completed, or all"),
    current_user: User = Depends(require_current_user),
    client: CupsClient = Depends(get_cups_client),
    database: Database = Depends(get_database),
) -> dict[str, object]:
    if scope not in JOB_SCOPE_TO_CUPS:
        raise HTTPException(status_code=400, detail="Invalid job scope")
    try:
        claims = get_user_job_claims(database, current_user)
        user_jobs = filter_jobs_by_owner(client.list_jobs("all"), claims, client.queue_name)
        jobs_by_scope = {
            "active": [job for job in user_jobs if job["is_active"]],
            "completed": [job for job in user_jobs if job["is_terminal"]],
            "all": user_jobs,
        }
        return {
            "scope": scope,
            "queue": client.queue_name,
            "jobs": jobs_by_scope[scope],
            "counts": {name: len(jobs) for name, jobs in jobs_by_scope.items()},
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
    database: Database = Depends(get_database),
) -> dict[str, object]:
    try:
        job = client.get_job(job_id)
    except CupsClientError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    require_user_cups_job(database, current_user, job, client.queue_name)
    return job


@app.delete("/jobs/{job_id}")
def cancel_job(
    job_id: int,
    current_user: User = Depends(require_current_user),
    client: CupsClient = Depends(get_cups_client),
    database: Database = Depends(get_database),
) -> dict[str, object]:
    try:
        job = client.get_job(job_id)
        require_user_cups_job(database, current_user, job, client.queue_name)
        result = client.cancel_job(job_id)
    except CupsClientError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    claim = claim_for_job(current_user.id, job, client.queue_name)
    if claim is not None:
        if result.get("cancelled"):
            database.update_print_history_status_for_claim(claim, "cancel-requested")
        elif history_cups_state(job) is not None:
            database.update_print_history_status_for_claim(claim, history_cups_state(job))
    return result


@app.post("/jobs/{job_id}/forget")
def forget_job(
    job_id: int,
    current_user: User = Depends(require_current_user),
    client: CupsClient = Depends(get_cups_client),
    database: Database = Depends(get_database),
) -> dict[str, object]:
    try:
        job = client.get_job(job_id)
        require_user_cups_job(database, current_user, job, client.queue_name)
        result = client.forget_job(job_id)
    except CupsClientError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if result.get("forgotten") is False:
        raise HTTPException(status_code=409, detail=result)
    claim = claim_for_job(current_user.id, job, client.queue_name)
    if claim is not None:
        database.update_print_history_status_for_claim(claim, "forgotten")
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
        with storage.lease(record.file_id):
            paths = preview_service.ensure_previews(record)
            return {
                "file_id": record.file_id,
                "page_count": record.page_count,
                "pages": [
                    {
                        "page": index,
                        "url": f"/files/{record.file_id}/preview/{index}",
                        **({"size_bytes": path.stat().st_size} if path.exists() else {}),
                    }
                    for index, path in enumerate(paths, start=1)
                ],
            }
    except (PreviewError, StorageError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


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
        lease = storage.lease(record.file_id)
        lease.__enter__()
        try:
            path = preview_service.preview_path(record, page_number)
        except Exception:
            lease.__exit__(None, None, None)
            raise
    except (PreviewError, StorageError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return FileResponse(path, media_type="image/png", background=BackgroundTask(lease.__exit__, None, None, None))


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
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(require_current_user),
    database: Database = Depends(get_database),
    client: CupsClient = Depends(get_cups_client),
) -> dict[str, object]:
    return {
        "history": refreshed_user_history(database, current_user, client, limit=limit, offset=offset),
        "total": database.count_print_history(current_user.id),
        "limit": limit,
        "offset": offset,
    }


@app.get("/history/{history_id}")
def get_history_entry(
    history_id: int,
    current_user: User = Depends(require_current_user),
    database: Database = Depends(get_database),
    client: CupsClient = Depends(get_cups_client),
) -> dict[str, object]:
    entries = refreshed_user_history(database, current_user, client, history_id)
    if not entries:
        raise HTTPException(status_code=404, detail="History entry not found")
    return entries[0]


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


def claim_for_job(user_id: int, job: dict[str, Any] | None, queue_name: str) -> JobClaim | None:
    if job is None:
        return None
    printer_uri = job.get("printer_uri")
    created_at = job.get("created_at")
    if not isinstance(printer_uri, str) or not printer_uri:
        return None
    try:
        uri = urlsplit(printer_uri)
    except ValueError:
        return None
    if (
        uri.scheme not in {"ipp", "ipps"}
        or not uri.netloc
        or unquote(uri.path).rstrip("/") != f"/printers/{queue_name}"
    ):
        return None
    try:
        created = int(created_at)
    except (TypeError, ValueError):
        return None
    if isinstance(created_at, bool) or created <= 0:
        return None
    uuid = job.get("job_uuid")
    return JobClaim(user_id, int(job["job_id"]), printer_uri, created,
                    str(uuid) if uuid else None)


def claim_matches_job(claim: JobClaim, job: dict[str, Any], queue_name: str) -> bool:
    actual = claim_for_job(claim.user_id, job, queue_name)
    return actual is not None and (
        actual.job_id == claim.job_id
        and actual.printer_uri == claim.printer_uri
        and actual.created_at == claim.created_at
        and actual.job_uuid == claim.job_uuid
    )


def get_user_job_claims(database: Database, current_user: User) -> list[JobClaim]:
    try:
        return database.list_job_claims(current_user.id)
    except Exception as exc:
        LOGGER.warning("Failed to read job claims for user %s: %s", current_user.id, exc)
        try:
            return database.list_fallback_job_claims(current_user.id)
        except Exception:
            LOGGER.exception("Failed to read fallback job claims for user %s", current_user.id)
            return []


def history_cups_state(job: dict[str, Any] | None) -> str | None:
    state = job.get("state") if job is not None else None
    return state if isinstance(state, str) and state in HISTORY_CUPS_STATES else None


def refreshed_user_history(
    database: Database, current_user: User, client: CupsClient,
    history_id: int | None = None,
    limit: int = 50, offset: int = 0,
) -> list[dict[str, Any]]:
    if history_id is None:
        history = database.list_print_history(current_user.id, limit, offset)
    else:
        entry = database.get_print_history(current_user.id, history_id)
        history = [entry] if entry is not None else []
    pending_ids = {
        row["cups_job_id"] for row in history
        if row["status"] not in HISTORY_TERMINAL_STATES
    }
    if not pending_ids:
        return history
    claims = [
        claim for claim in get_user_job_claims(database, current_user)
        if claim.job_id in pending_ids
    ]
    if not claims:
        return history
    try:
        jobs = {job["job_id"]: job for job in client.list_jobs("all")}
    except CupsClientError as exc:
        LOGGER.warning("Could not refresh print history from CUPS: %s", exc)
        return history
    changed = False
    for claim in claims:
        job = jobs.get(claim.job_id)
        state = history_cups_state(job)
        if job is not None and state is not None and claim_matches_job(claim, job, client.queue_name):
            database.update_print_history_status_for_claim(claim, state)
            changed = True
    if not changed:
        return history
    if history_id is None:
        return database.list_print_history(current_user.id, limit, offset)
    refreshed = database.get_print_history(current_user.id, history_id)
    return [refreshed] if refreshed is not None else []


def require_user_cups_job(
    database: Database, current_user: User, job: dict[str, Any] | None, queue_name: str,
) -> None:
    if job is None or not any(
        claim_matches_job(claim, job, queue_name)
        for claim in get_user_job_claims(database, current_user)
    ):
        raise HTTPException(status_code=404, detail="Job not found")


def filter_jobs_by_owner(
    jobs: list[dict[str, Any]],
    claims: list[JobClaim],
    queue_name: str,
) -> list[dict[str, Any]]:
    by_id: dict[int, list[JobClaim]] = {}
    for claim in claims:
        by_id.setdefault(claim.job_id, []).append(claim)
    return [
        job for job in jobs
        if any(
            claim_matches_job(claim, job, queue_name)
            for claim in by_id.get(job.get("job_id"), [])
        )
    ]


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
