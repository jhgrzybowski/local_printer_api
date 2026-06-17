from __future__ import annotations

from fastapi.testclient import TestClient


def signup_user(
    client: TestClient,
    username: str = "alice",
    password: str = "correct horse",
) -> dict[str, object]:
    response = client.post(
        "/auth/signup",
        json={"username": username, "password": password, "display_name": username.title()},
    )
    assert response.status_code == 200
    return response.json()
