from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.services.cups_client import CupsClient, CupsClientError, CupsJobChangedError, normalize_job


class RecordingConnection:
    def __init__(self, jobs: dict[int, dict[str, Any]]) -> None:
        self.jobs = jobs
        self.which_jobs: list[str] = []
        self.first_job_ids: list[int] = []
        self.cancelled: list[tuple[int, bool]] = []

    def getJobs(
        self,
        which_jobs: str = "not-completed",
        requested_attributes: list[str] | None = None,
        first_job_id: int = -1,
    ) -> dict[int, dict[str, Any]]:
        self.which_jobs.append(which_jobs)
        self.first_job_ids.append(first_job_id)
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
        def getPrinterAttributes(self, name: str, requested_attributes: list[str]) -> dict[str, Any]:
            raise RuntimeError(1030, "client-error-not-found")

    client._connection = lambda: Connection()  # type: ignore[method-assign]
    with pytest.raises(CupsClientError, match="does not exist"):
        client.get_option_capabilities()


@pytest.mark.parametrize("accepting_jobs", [True, False])
def test_get_queue_queries_accepting_jobs_explicitly(
    monkeypatch: pytest.MonkeyPatch, accepting_jobs: bool,
) -> None:
    printer_uri = "ipp://localhost/printers/Canon_MG5350"

    class Connection:
        def getPrinters(self) -> dict[str, Any]:
            return {
                "Canon_MG5350": {
                    "device-uri": "lpd://192.168.100.100/PASSTHRU",
                    "printer-state": 3,
                    "printer-uri-supported": [printer_uri],
                }
            }

        def getPrinterAttributes(
            self, *, uri: str, requested_attributes: list[str],
        ) -> dict[str, bool]:
            assert uri == printer_uri
            assert requested_attributes == ["printer-is-accepting-jobs"]
            return {"printer-is-accepting-jobs": accepting_jobs}

    client = CupsClient()
    client._connection = lambda: Connection()  # type: ignore[method-assign]
    monkeypatch.setattr(
        "app.services.cups_client.probe_printer_reachability",
        lambda *_, **__: {"checked": True, "reachable": True},
    )

    result = client.get_queue()

    assert result["attributes"]["printer-is-accepting-jobs"] is accepting_jobs


def test_get_queue_keeps_queue_snapshot_when_accepting_jobs_query_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Connection:
        def getPrinters(self) -> dict[str, Any]:
            return {
                "Canon_MG5350": {
                    "device-uri": "lpd://192.168.100.100/PASSTHRU",
                    "printer-state": 3,
                    "printer-uri-supported": ["ipp://localhost/printers/Canon_MG5350"],
                }
            }

        def getPrinterAttributes(self, **_: Any) -> dict[str, bool]:
            raise RuntimeError("attribute query unavailable")

    client = CupsClient()
    client._connection = lambda: Connection()  # type: ignore[method-assign]
    monkeypatch.setattr(
        "app.services.cups_client.probe_printer_reachability",
        lambda *_, **__: {"checked": True, "reachable": True},
    )

    result = client.get_queue()

    assert result["exists"] is True
    assert "printer-is-accepting-jobs" not in result["attributes"]


MINIMAL_PPD = (
    "*NickName: \"Test Printer\"\n"
    "*OpenUI *PageSize/Media Size: PickOne\n"
    "*DefaultPageSize: A4\n"
    "*PageSize A4/A4: \"<</PageSize[595 842]>>setpagedevice\"\n"
    "*CloseUI: *PageSize\n"
    "*PaperDimension A4/A4: \"595 842\"\n"
)


class ProfileConnection:
    """CUPS answering for one queue with a PPD and IPP job attributes."""

    def __init__(self, tmp_path: Path, ppd: str | None = MINIMAL_PPD) -> None:
        self.tmp_path = tmp_path
        self.ppd = ppd
        self.ppd_requests: list[float | None] = []
        self.attribute_requests = 0
        self.written: list[Path] = []

    def getPrinterAttributes(self, name: str, requested_attributes: list[str]) -> dict[str, Any]:
        self.attribute_requests += 1
        return {
            "printer-config-change-time": 10,
            "print-quality-supported": [4, 5],
            "number-up-supported": [1, 2, 4],
            "multiple-document-handling-supported": ["separate-documents-collated-copies"],
        }

    def getPPD3(self, name: str, modtime: float | None = None) -> tuple[int, float, str]:
        self.ppd_requests.append(modtime)
        path = self.tmp_path / f"ppd-{len(self.ppd_requests)}"
        path.write_text(self.ppd or "", encoding="utf-8")
        self.written.append(path)
        if self.ppd is None:
            return 404, 0.0, str(path)
        if modtime == 50.0:
            return 304, 50.0, str(path)
        return 200, 50.0, str(path)


def test_capabilities_merge_ppd_options_with_ipp_job_attributes(tmp_path: Path) -> None:
    client = CupsClient("Canon_MG5350")
    connection = ProfileConnection(tmp_path)
    client._connection = lambda: connection  # type: ignore[method-assign]

    profile = client.get_printer_profile()

    assert profile.capabilities["PageSize"] == {"A4"}
    assert profile.capabilities["print-quality"] == {"4", "5"}
    assert profile.capabilities["number-up"] == {"1", "2", "4"}
    assert profile.capabilities["collate"] == {"true", "false"}
    assert profile.ppd is not None and profile.ppd.nickname == "Test Printer"
    assert profile.ppd.paper_sizes["A4"].width == 595
    # pycups leaves a temporary PPD copy behind; the client removes it.
    assert not any(path.exists() for path in connection.written)


def test_capabilities_are_cached_and_revalidated_with_a_304(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import cups_client

    client = CupsClient("Canon_MG5350")
    connection = ProfileConnection(tmp_path)
    client._connection = lambda: connection  # type: ignore[method-assign]
    clock = [1000.0]
    monkeypatch.setattr(cups_client.time, "monotonic", lambda: clock[0])

    first = client.get_printer_profile()
    client.get_printer_profile()
    assert connection.ppd_requests == [None]
    assert connection.attribute_requests == 1

    clock[0] += cups_client.CAPABILITY_CACHE_SECONDS + 1
    again = client.get_printer_profile()

    assert connection.ppd_requests == [None, 50.0]
    assert again.ppd is first.ppd
    assert not any(path.exists() for path in connection.written)


def test_capabilities_fall_back_to_lpoptions_without_a_ppd(tmp_path: Path) -> None:
    client = CupsClient("Canon_MG5350")
    client._connection = lambda: ProfileConnection(tmp_path, ppd=None)  # type: ignore[method-assign]
    client._get_lpoptions_capabilities = lambda: {"PageSize": {"Letter"}}  # type: ignore[method-assign]

    profile = client.get_printer_profile()

    assert profile.ppd is None
    assert profile.capabilities["PageSize"] == {"Letter"}
    assert profile.capabilities["print-quality"] == {"4", "5"}


def test_capabilities_fail_when_every_source_is_empty(tmp_path: Path) -> None:
    client = CupsClient("Canon_MG5350")

    class EmptyConnection(ProfileConnection):
        def getPrinterAttributes(self, name: str, requested_attributes: list[str]) -> dict[str, Any]:
            return {}

    client._connection = lambda: EmptyConnection(tmp_path, ppd=None)  # type: ignore[method-assign]

    def unavailable_lpoptions() -> dict[str, set[str]]:
        raise CupsClientError("lpoptions failed")

    client._get_lpoptions_capabilities = unavailable_lpoptions  # type: ignore[method-assign]
    with pytest.raises(CupsClientError, match="capability detection failed"):
        client.get_option_capabilities()


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
    assert connection.first_job_ids == [-1]
    assert [job["job_id"] for job in jobs] == [1]


def test_list_jobs_passes_lower_job_id_bound() -> None:
    connection = RecordingConnection({})
    client = CupsClient()
    client._connection = lambda: connection  # type: ignore[method-assign]

    client.list_jobs("all", first_job_id=40)

    assert connection.first_job_ids == [40]


def test_list_jobs_fetches_details_when_bulk_snapshot_omits_uuid() -> None:
    snapshot = {
        "job-name": "finished.pdf",
        "job-state": 9,
        "job-printer-uri": "ipp://localhost/printers/Canon_MG5350",
        "time-at-creation": 1000,
    }
    detailed = {**snapshot, "job-uuid": "urn:uuid:job-21"}

    class Connection:
        detail_queries = 0

        def getJobs(
            self,
            which_jobs: str = "not-completed",
            requested_attributes: list[str] | None = None,
            first_job_id: int = -1,
        ) -> dict[int, dict[str, Any]]:
            assert which_jobs == "all"
            assert requested_attributes is not None and "job-uuid" in requested_attributes
            return {21: snapshot}

        def getJobAttributes(self, job_id: int) -> dict[str, Any]:
            assert job_id == 21
            self.detail_queries += 1
            return detailed

    connection = Connection()
    client = CupsClient()
    client._connection = lambda: connection  # type: ignore[method-assign]

    jobs = client.list_jobs("all")

    assert jobs[0]["job_uuid"] == "urn:uuid:job-21"
    assert jobs[0]["state"] == "completed"
    assert connection.detail_queries == 1


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


def test_normalize_job_reports_live_progress() -> None:
    job = normalize_job(7, {
        "job-state": 5,
        "time-at-processing": 1700,
        "job-impressions-completed": 2,
        "job-media-sheets-completed": 1,
        "job-media-progress": 45,
        "job-printer-state-message": "Printing page 3",
    })

    assert (job["pages_completed"], job["sheets_completed"], job["media_progress"]) == (2, 1, 45)
    assert job["state_message"] == "Printing page 3"
    assert job["processing_at"] == 1700
