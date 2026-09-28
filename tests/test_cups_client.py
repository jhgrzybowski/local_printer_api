from __future__ import annotations

from typing import Any

import pytest

from app.services.cups_client import CupsClient, CupsClientError, CupsJobChangedError, normalize_job


class RecordingConnection:
    def __init__(self, jobs: dict[int, dict[str, Any]]) -> None:
        self.jobs = jobs
        self.which_jobs: list[str] = []
        self.cancelled: list[tuple[int, bool]] = []

    def getJobs(
        self,
        which_jobs: str = "not-completed",
        requested_attributes: list[str] | None = None,
    ) -> dict[int, dict[str, Any]]:
        self.which_jobs.append(which_jobs)
        if which_jobs == "not-completed":
            return {
                job_id: attrs
                for job_id, attrs in self.jobs.items()
                if attrs.get("job-state") in {3, 4, 5, 6}
            }
        if which_jobs == "completed":
            return {
                job_id: attrs
                for job_id, attrs in self.jobs.items()
                if attrs.get("job-state") in {7, 8, 9}
            }
        return self.jobs

    def getJobAttributes(self, job_id: int) -> dict[str, Any]:
        return self.jobs[job_id]

    def cancelJob(self, job_id: int, purge_job: bool = False) -> None:
        self.cancelled.append((job_id, purge_job))


class NotPossibleConnection(RecordingConnection):
    def cancelJob(self, job_id: int, purge_job: bool = False) -> None:
        raise RuntimeError("(1028, 'client-error-not-possible')")


def test_capabilities_fail_when_queue_is_missing() -> None:
    client = CupsClient("Canon_MG5350")

    class Connection:
        def getPrinters(self) -> dict[str, Any]:
            return {}

    client._connection = lambda: Connection()  # type: ignore[method-assign]
    with pytest.raises(CupsClientError, match="does not exist"):
        client.get_option_capabilities()


def test_capabilities_fail_when_both_sources_fail() -> None:
    client = CupsClient("Canon_MG5350")

    class Connection:
        def getPrinters(self) -> dict[str, Any]:
            return {"Canon_MG5350": {}}

    client._connection = lambda: Connection()  # type: ignore[method-assign]
    client._get_lpoptions_capabilities = lambda: {}  # type: ignore[method-assign]
    client._get_ppd_capabilities = lambda: {}  # type: ignore[method-assign]
    with pytest.raises(CupsClientError, match="capability detection failed"):
        client.get_option_capabilities()


def test_capabilities_use_ppd_when_lpoptions_fails() -> None:
    client = CupsClient("Canon_MG5350")

    class Connection:
        def getPrinters(self) -> dict[str, Any]:
            return {"Canon_MG5350": {}}

    client._connection = lambda: Connection()  # type: ignore[method-assign]

    def unavailable_lpoptions() -> dict[str, set[str]]:
        raise CupsClientError("lpoptions failed")

    client._get_lpoptions_capabilities = unavailable_lpoptions  # type: ignore[method-assign]
    client._get_ppd_capabilities = lambda: {"PageSize": {"A4"}}  # type: ignore[method-assign]
    assert client.get_option_capabilities() == {"PageSize": {"A4"}}


def test_list_jobs_maps_active_scope_to_not_completed() -> None:
    connection = RecordingConnection(
        {
            1: {"job-name": "active.pdf", "job-state": 5},
            2: {"job-name": "done.pdf", "job-state": 9},
        }
    )
    client = CupsClient()
    client._connection = lambda: connection  # type: ignore[method-assign]

    jobs = client.list_jobs("active")

    assert connection.which_jobs == ["not-completed"]
    assert [job["job_id"] for job in jobs] == [1]


def test_normalize_job_marks_active_and_terminal_states() -> None:
    active = normalize_job(1, {"job-state": 6})
    terminal = normalize_job(2, {"job-state": 9})
    unknown = normalize_job(3, {})

    assert active["state"] == "processing-stopped"
    assert active["is_active"] is True
    assert active["can_cancel"] is True
    assert terminal["is_terminal"] is True
    assert terminal["can_cancel"] is False
    assert terminal["can_forget"] is True
    assert unknown["state"] == "unknown"
    assert unknown["can_cancel"] is False


def test_cancel_terminal_job_does_not_call_cups_cancel() -> None:
    connection = RecordingConnection({1: {"job-name": "done.pdf", "job-state": 9}})
    client = CupsClient()
    client._connection = lambda: connection  # type: ignore[method-assign]

    response = client.cancel_job(1)

    assert response["cancelled"] is False
    assert response["already_terminal"] is True
    assert response["can_forget"] is True
    assert connection.cancelled == []


def test_cancel_not_possible_is_translated_to_domain_response() -> None:
    connection = NotPossibleConnection({1: {"job-name": "active.pdf", "job-state": 5}})
    client = CupsClient()
    client._connection = lambda: connection  # type: ignore[method-assign]

    response = client.cancel_job(1)

    assert response["job_id"] == 1
    assert response["cancelled"] is False
    assert response["message"] == "CUPS says this job cannot be cancelled."


def test_forget_terminal_job_uses_pycups_purge_flag() -> None:
    connection = RecordingConnection({1: {"job-name": "done.pdf", "job-state": 9}})
    client = CupsClient()
    client._connection = lambda: connection  # type: ignore[method-assign]

    response = client.forget_job(1)

    assert response == {"job_id": 1, "forgotten": True, "method": "pycups-purge-job"}
    assert connection.cancelled == [(1, True)]


@pytest.mark.parametrize("action, old_state, new_state", [
    ("cancel_job", 5, 5),
    ("forget_job", 9, 9),
])
def test_job_reuse_between_authorization_and_action_is_rejected(
    action: str, old_state: int, new_state: int,
) -> None:
    old = {
        "job-state": old_state,
        "job-printer-uri": "ipp://localhost/printers/Canon_MG5350",
        "time-at-creation": 1000,
        "job-uuid": "urn:uuid:old",
    }
    connection = RecordingConnection({1: old})
    client = CupsClient()
    client._connection = lambda: connection  # type: ignore[method-assign]
    authorized_job = client.get_job(1)
    assert authorized_job is not None

    connection.jobs[1] = {
        **old,
        "job-state": new_state,
        "job-uuid": "urn:uuid:replacement",
    }
    with pytest.raises(CupsJobChangedError):
        getattr(client, action)(1, expected_job=authorized_job)

    assert connection.cancelled == []
