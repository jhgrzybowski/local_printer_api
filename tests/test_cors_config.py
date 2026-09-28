from pathlib import Path

from app.settings import DEFAULT_CORS_ALLOWED_ORIGINS, _env_csv


COMPOSE_FILE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


def test_compose_uses_application_cors_defaults_without_override(monkeypatch) -> None:
    compose = COMPOSE_FILE.read_text()
    assert "CORS_ALLOWED_ORIGINS: ${CORS_ALLOWED_ORIGINS:-}" in compose

    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    assert _env_csv("CORS_ALLOWED_ORIGINS", DEFAULT_CORS_ALLOWED_ORIGINS) == list(
        DEFAULT_CORS_ALLOWED_ORIGINS
    )


def test_compose_cors_override_remains_configurable(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://custom.local, http://other.local")
    assert _env_csv("CORS_ALLOWED_ORIGINS", DEFAULT_CORS_ALLOWED_ORIGINS) == [
        "https://custom.local",
        "http://other.local",
    ]
