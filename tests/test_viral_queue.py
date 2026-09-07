"""Scheduling, retry, and the worker's per-job account isolation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from viral.publishing import MAX_ATTEMPTS, PublishRequest, PublishResult, PublishStatus
from viral.queue import (
    JobQueue,
    PublishJob,
    QueueError,
    queue_summary,
    record_result,
    run_due_jobs,
)

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
ALICE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BOB = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture
def queue():
    return JobQueue()


def _request(user_id=ALICE, **over):
    params = dict(user_id=user_id, video_id="v1", media_path="vrf/u/v1.mp4",
                  caption="A caption", platforms=("youtube",))
    params.update(over)
    return PublishRequest(**params)


def _ok(platform="youtube"):
    return PublishResult(platform, PublishStatus.PUBLISHED,
                         post_id="p1", url="https://y/p1")


def _transient(platform="youtube"):
    return PublishResult(platform, PublishStatus.FAILED,
                         error="502 Bad Gateway", retryable=True)


def _permanent(platform="youtube"):
    return PublishResult(platform, PublishStatus.FAILED,
                         error="Invalid credentials", retryable=False)


# --- enqueue ---------------------------------------------------------------

def test_one_job_per_selected_platform(queue):
    jobs = queue.enqueue(_request(platforms=("youtube", "instagram")), now=NOW)
    assert {j.platform for j in jobs} == {"youtube", "instagram"}
    assert all(j.user_id == ALICE for j in jobs)


def test_an_immediate_publish_is_due_right_away(queue):
    job = queue.enqueue(_request(), now=NOW)[0]
    assert job.due(NOW) is True


def test_a_queue_scheduled_job_waits_for_its_time(queue):
    job = queue.enqueue(
        _request(platforms=("instagram",), scheduled_for=NOW + timedelta(hours=6)),
        now=NOW)[0]
    assert job.mode == "queue_until_due"
    assert job.due(NOW) is False
    assert job.due(NOW + timedelta(hours=6)) is True


def test_a_natively_scheduled_job_uploads_now(queue):
    """YouTube is told the time at upload, so the job itself must not wait."""
    job = queue.enqueue(
        _request(scheduled_for=NOW + timedelta(days=2)), now=NOW)[0]
    assert job.mode == "native_schedule"
    assert job.due(NOW) is True


def test_an_unsupported_platform_is_stored_as_finished_not_pending(queue):
    job = queue.enqueue(_request(platforms=("snapchat",)), now=NOW)[0]
    assert job.status == PublishStatus.UNSUPPORTED.value
    assert job.terminal is True
    assert job.due(NOW) is False
    assert job.last_error


def test_a_past_schedule_is_refused_at_enqueue(queue):
    with pytest.raises(QueueError, match="past"):
        queue.enqueue(_request(scheduled_for=NOW - timedelta(hours=1)), now=NOW)


def test_no_platform_is_refused_at_enqueue(queue):
    with pytest.raises(QueueError):
        queue.enqueue(_request(platforms=()), now=NOW)


# --- isolation -------------------------------------------------------------

def test_a_user_only_sees_their_own_jobs(queue):
    queue.enqueue(_request(ALICE), now=NOW)
    queue.enqueue(_request(BOB, platforms=("youtube", "instagram")), now=NOW)

    assert len(queue.list_for_user(ALICE)) == 1
    assert len(queue.list_for_user(BOB)) == 2


def test_reading_another_users_job_is_refused(queue):
    job = queue.enqueue(_request(ALICE), now=NOW)[0]
    with pytest.raises(QueueError, match="different user"):
        queue.get(BOB, job.id)


def test_cancelling_another_users_job_is_refused(queue):
    job = queue.enqueue(_request(ALICE), now=NOW)[0]
    with pytest.raises(QueueError, match="different user"):
        queue.cancel(BOB, job.id, now=NOW)
    assert queue.get(ALICE, job.id).status == PublishStatus.QUEUED.value


def test_the_worker_uses_each_jobs_own_owner_for_the_token(queue):
    """The one rule that, if broken, posts to a stranger's account."""
    queue.enqueue(_request(ALICE), now=NOW)
    queue.enqueue(_request(BOB), now=NOW)
    seen = []

    def token_for(user_id, platform):
        seen.append(user_id)
        return f"token-{user_id[:4]}"

    def publisher(job, token):
        assert token == f"token-{job.user_id[:4]}", "token belonged to another user"
        return _ok(job.platform)

    run_due_jobs(queue, publisher=publisher, token_for=token_for, now=NOW)
    assert sorted(seen) == sorted([ALICE, BOB])


def test_a_summary_counts_only_this_users_jobs(queue):
    queue.enqueue(_request(ALICE, platforms=("youtube", "instagram")), now=NOW)
    queue.enqueue(_request(BOB), now=NOW)
    summary = queue_summary(queue, ALICE)
    assert summary["total"] == 2
    assert summary["by_status"][PublishStatus.QUEUED.value] == 2


# --- results and retry -----------------------------------------------------

def test_a_success_is_terminal_and_carries_the_link(queue):
    job = queue.enqueue(_request(), now=NOW)[0]
    record_result(job, _ok(), now=NOW)
    assert job.status == PublishStatus.PUBLISHED.value
    assert job.post_url == "https://y/p1"
    assert job.terminal is True
    assert job.due(NOW + timedelta(days=1)) is False


def test_a_transient_failure_is_rescheduled_with_backoff(queue):
    job = queue.enqueue(_request(), now=NOW)[0]
    record_result(job, _transient(), now=NOW)
    assert job.status == PublishStatus.QUEUED.value
    assert job.attempts == 1
    assert job.next_attempt_at > NOW
    assert job.due(NOW) is False


def test_backoff_grows_between_attempts(queue):
    job = queue.enqueue(_request(), now=NOW)[0]
    record_result(job, _transient(), now=NOW)
    first = job.next_attempt_at
    record_result(job, _transient(), now=NOW)
    assert job.next_attempt_at > first


def test_a_permanent_failure_never_retries(queue):
    """Repeating a rejected request only burns the user's rate limit."""
    job = queue.enqueue(_request(), now=NOW)[0]
    record_result(job, _permanent(), now=NOW)
    assert job.status == PublishStatus.FAILED.value
    assert job.next_attempt_at is None
    assert job.terminal is True


def test_retries_stop_at_the_attempt_limit(queue):
    job = queue.enqueue(_request(), now=NOW)[0]
    for _ in range(MAX_ATTEMPTS):
        record_result(job, _transient(), now=NOW)
    assert job.attempts == MAX_ATTEMPTS
    assert job.status == PublishStatus.FAILED.value
    assert "gave up" in job.last_error
    assert job.terminal is True


def test_a_publish_with_a_caveat_keeps_the_note(queue):
    """A TikTok draft is a real outcome the user has to act on."""
    job = queue.enqueue(_request(platforms=("tiktok",)), now=NOW)[0]
    record_result(job, PublishResult(
        "tiktok", PublishStatus.PUBLISHED, post_id="tt-1",
        error="Sent to the TikTok app's drafts."), now=NOW)
    assert job.status == PublishStatus.PUBLISHED.value
    assert "drafts" in job.last_error


def test_a_cancelled_job_stops(queue):
    job = queue.enqueue(
        _request(platforms=("instagram",), scheduled_for=NOW + timedelta(days=1)),
        now=NOW)[0]
    queue.cancel(ALICE, job.id, now=NOW)
    assert job.terminal is True
    assert queue.due(NOW + timedelta(days=2)) == []


def test_a_finished_job_cannot_be_cancelled(queue):
    job = queue.enqueue(_request(), now=NOW)[0]
    record_result(job, _ok(), now=NOW)
    with pytest.raises(QueueError, match="already finished"):
        queue.cancel(ALICE, job.id, now=NOW)


# --- the worker loop -------------------------------------------------------

def test_only_due_jobs_run(queue):
    queue.enqueue(_request(), now=NOW)
    queue.enqueue(
        _request(platforms=("instagram",), scheduled_for=NOW + timedelta(days=1)),
        now=NOW)

    ran = run_due_jobs(
        queue, publisher=lambda job, token: _ok(job.platform),
        token_for=lambda u, p: "t", now=NOW)
    assert [j.platform for j in ran] == ["youtube"]


def test_a_missing_account_fails_the_job_without_spinning(queue):
    from viral.accounts import AccountError

    queue.enqueue(_request(), now=NOW)

    def token_for(user_id, platform):
        raise AccountError("No connected youtube account.")

    ran = run_due_jobs(
        queue, publisher=lambda job, token: _ok(),
        token_for=token_for, now=NOW)
    assert ran[0].status == PublishStatus.FAILED.value
    assert ran[0].next_attempt_at is None
    assert "No connected" in ran[0].last_error


def test_a_publisher_crash_does_not_stop_the_other_jobs(queue):
    queue.enqueue(_request(ALICE), now=NOW)
    queue.enqueue(_request(BOB), now=NOW)

    def publisher(job, token):
        if job.user_id == ALICE:
            raise RuntimeError("adapter blew up")
        return _ok()

    ran = run_due_jobs(queue, publisher=publisher,
                       token_for=lambda u, p: "t", now=NOW)
    outcomes = {j.user_id: j.status for j in ran}
    assert outcomes[ALICE] == PublishStatus.FAILED.value
    assert outcomes[BOB] == PublishStatus.PUBLISHED.value


def test_the_queue_record_carries_no_media_path_or_token(queue):
    job = queue.enqueue(_request(), now=NOW)[0]
    record = job.to_record()
    assert "media_path" not in record
    assert "caption" not in record
    assert record["platform"] == "youtube"


def test_due_jobs_run_oldest_first(queue):
    later = queue.enqueue(
        _request(platforms=("instagram",), scheduled_for=NOW + timedelta(hours=2)),
        now=NOW)[0]
    sooner = queue.enqueue(
        _request(platforms=("tiktok",), scheduled_for=NOW + timedelta(hours=1)),
        now=NOW)[0]
    order = [j.id for j in queue.due(NOW + timedelta(hours=3))]
    assert order == [sooner.id, later.id]
