from collections.abc import Iterator

import pytest
import yaml
from fastapi.testclient import TestClient

from app.main import app, get_cups_client
from app.services.cups_client import CupsClientError


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides.clear()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class FakeCupsClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def get_queue(self) -> dict[str, object]:
        return self.payload


class FailingCupsClient:
    def get_queue(self) -> dict[str, object]:
        raise CupsClientError("CUPS query failed")


def test_health(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "local-printer-api",
    }


def test_health_includes_cors_header_for_allowed_origin(client: TestClient) -> None:
    origin = "http://192.168.100.99:5173"

    response = client.get("/health", headers={"Origin": origin})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize(
    "origin",
    [
        "http://192.168.100.99:5173",
        "http://drukarka.local:5174",
        "https://drukarka.local",
    ],
)
def test_files_preflight_includes_cors_headers_for_allowed_origin(
    client: TestClient, origin: str
) -> None:

    response = client.options(
        "/files",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "Content-Type" in response.headers["access-control-allow-headers"]


def test_openapi_yaml_is_served(client: TestClient) -> None:
    response = client.get("/openapi.yaml")

    assert response.status_code == 200
    assert "application/yaml" in response.headers["content-type"]
    assert response.text.startswith("openapi: 3.1.0")


def test_openapi_json_matches_canonical_yaml(client: TestClient) -> None:
    yaml_response = client.get("/openapi.yaml")
    json_response = client.get("/openapi.json")

    assert json_response.status_code == 200
    assert "application/json" in json_response.headers["content-type"]
    assert json_response.json() == yaml.safe_load(yaml_response.text)
    assert app.openapi() == json_response.json()


def test_docs_uses_json_version_of_canonical_spec(client: TestClient) -> None:
    response = client.get("/docs")

    assert response.status_code == 200
    assert "url: '/openapi.json'" in response.text


def test_status_uses_cups_client_dependency(client: TestClient) -> None:
    app.dependency_overrides[get_cups_client] = lambda: FakeCupsClient(
        {
            "name": "Canon_MG5350",
            "exists": True,
            "attributes": {
                "printer-state": 4,
                "printer-is-accepting-jobs": True,
                "device-uri": "ipp://192.168.100.100/ipp/print",
            },
        }
    )

    response = client.get("/status")

    assert response.status_code == 200
    body = response.json()
    assert body["queue_name"] == "Canon_MG5350"
    assert body["exists"] is True
    assert body["state"] == "processing"
    assert body["cups"] == {"available": True, "error": None}


def test_status_reports_cups_failure_without_raising(client: TestClient) -> None:
    app.dependency_overrides[get_cups_client] = lambda: FailingCupsClient()

    response = client.get("/status")

    assert response.status_code == 200
    body = response.json()
    assert body["queue_name"] == "Canon_MG5350"
    assert body["state"] == "unknown"
    assert body["cups"] == {"available": False, "error": "CUPS query failed"}
