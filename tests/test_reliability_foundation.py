"""Offline contracts for checkpoint recovery and artifact safety."""

from pathlib import Path

from PIL import Image

from core.reliability import RunAttempt, TerminalState, deterministic_workspace, fallback_contract, promote_artifact


def _valid_image(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except Exception:
        return False


def test_workspace_is_deterministic_for_a_job(tmp_path):
    first = deterministic_workspace(tmp_path, "football_news", "12345678-abcd")
    second = deterministic_workspace(tmp_path, "football_news", "12345678-abcd")
    assert first == second
    assert "job_12345678-abcd" in first.name


def test_resume_is_bounded_state_not_a_new_attempt_id():
    attempt = RunAttempt()
    attempt_id = attempt.attempt_id
    attempt.begin_resume()
    assert attempt.attempt_id == attempt_id
    assert attempt.retry_count == 1
    assert attempt.terminal_state == TerminalState.RUNNING


def test_provider_timeout_and_rate_limit_are_recorded_as_recoverable():
    attempt = RunAttempt()
    attempt.record_provider_failure("provider", "image", "timeout")
    attempt.record_provider_failure("provider", "image", "rate limit")
    assert len(attempt.provider_attempts) == 2
    assert all(item.recoverable for item in attempt.provider_attempts)


def test_failed_redraw_does_not_delete_last_valid_artifact(tmp_path):
    destination = tmp_path / "frame.png"
    Image.new("RGB", (8, 8), "green").save(destination)
    candidate = tmp_path / "candidate.png"
    candidate.write_bytes(b"not an image")
    assert promote_artifact(candidate, destination, _valid_image) is False
    assert _valid_image(destination)


def test_valid_redraw_replaces_artifact_atomically(tmp_path):
    destination = tmp_path / "frame.png"
    Image.new("RGB", (8, 8), "green").save(destination)
    candidate = tmp_path / "candidate.png"
    Image.new("RGB", (8, 8), "blue").save(candidate)
    assert promote_artifact(candidate, destination, _valid_image) is True
    assert _valid_image(destination)
    assert not candidate.exists()


def test_fallback_contract_never_requires_an_invalid_fallback():
    image = fallback_contract("image_source")
    assert image.primary
    assert image.retry is True
    assert image.continue_on_exhaustion is True
    assert fallback_contract("unknown").primary == "existing stage implementation"


def test_worker_uses_job_identity_not_newest_workspace_scan():
    worker = (Path(__file__).resolve().parents[1] / "worker" / "worker.py").read_text(encoding="utf-8")
    run = worker[worker.index("def run_pipeline("):worker.index("def _release_run_scratch")]
    assert "workspace_for_job(job)" in run
    assert "_newest_new_workspace(before)" not in run
    assert '"--run-id", job_id' in run


def test_completion_manifest_makes_upload_retry_idempotent():
    worker = (Path(__file__).resolve().parents[1] / "worker" / "worker.py").read_text(encoding="utf-8")
    assert "completion_manifest.json" in worker
    assert "reusing verified upload from completion manifest" in worker
