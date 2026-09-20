from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from drawbridge.storage import Database, IdempotencyConflict, QueueFull, create_request_hash


@pytest.mark.asyncio
async def test_queue_is_idempotent_and_rejects_same_key_with_different_payload(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()

    first = await database.enqueue_job(
        action="demo",
        app="demo",
        environment="staging",
        payload={"value": 1},
        idempotency_key="idem-0001",
        request_hash=create_request_hash({"value": 1}),
        max_queued_jobs=50,
        max_queued_jobs_per_target=5,
    )
    duplicate = await database.enqueue_job(
        action="demo",
        app="demo",
        environment="staging",
        payload={"value": 1},
        idempotency_key="idem-0001",
        request_hash=create_request_hash({"value": 1}),
        max_queued_jobs=50,
        max_queued_jobs_per_target=5,
    )

    assert first.job_id == duplicate.job_id
    assert duplicate.created is False

    with pytest.raises(IdempotencyConflict):
        await database.enqueue_job(
            action="demo",
            app="demo",
            environment="staging",
            payload={"value": 2},
            idempotency_key="idem-0001",
            request_hash=create_request_hash({"value": 2}),
            max_queued_jobs=50,
            max_queued_jobs_per_target=5,
        )
    await database.close()


@pytest.mark.asyncio
async def test_queue_enforces_global_and_target_capacity(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()

    for number in range(2):
        await database.enqueue_job(
            action="demo",
            app="demo",
            environment="staging",
            payload={"number": number},
            idempotency_key=f"idem-{number:04d}",
            request_hash=create_request_hash({"number": number}),
            max_queued_jobs=3,
            max_queued_jobs_per_target=2,
        )

    with pytest.raises(QueueFull):
        await database.enqueue_job(
            action="demo",
            app="demo",
            environment="staging",
            payload={"number": 2},
            idempotency_key="idem-0002",
            request_hash=create_request_hash({"number": 2}),
            max_queued_jobs=3,
            max_queued_jobs_per_target=2,
        )

    second_target = await database.enqueue_job(
        action="demo",
        app="other",
        environment="staging",
        payload={"number": 3},
        idempotency_key="idem-0003",
        request_hash=create_request_hash({"number": 3}),
        max_queued_jobs=3,
        max_queued_jobs_per_target=2,
    )
    assert second_target.created is True

    claimed = await database.claim_next_job(owner="runner-1")
    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.app == "demo"
    await database.close()


@pytest.mark.asyncio
async def test_queue_timeout_expires_without_claiming(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()
    job = await database.enqueue_job(
        action="demo",
        app="demo",
        environment="staging",
        payload={},
        idempotency_key="idem-0001",
        request_hash=create_request_hash({}),
        max_queued_jobs=50,
        max_queued_jobs_per_target=5,
        queue_timeout_seconds=0,
    )

    await asyncio.sleep(0)
    assert await database.claim_next_job(owner="runner-1") is None
    expired = await database.get_job(job.job_id)
    assert expired is not None
    assert expired.status == "queue_expired"
    await database.close()


@pytest.mark.asyncio
async def test_claim_respects_global_running_slot(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()
    first = await database.enqueue_job(
        action="demo",
        app="demo",
        environment="staging",
        payload={"number": 1},
        idempotency_key="idem-0001",
        request_hash=create_request_hash({"number": 1}),
        max_queued_jobs=50,
        max_queued_jobs_per_target=5,
    )
    second = await database.enqueue_job(
        action="demo",
        app="demo",
        environment="staging",
        payload={"number": 2},
        idempotency_key="idem-0002",
        request_hash=create_request_hash({"number": 2}),
        max_queued_jobs=50,
        max_queued_jobs_per_target=5,
    )

    claimed = await database.claim_next_job(owner="runner-1", max_running_jobs=1)
    assert claimed is not None
    assert claimed.job_id == first.job_id
    assert await database.claim_next_job(owner="runner-2", max_running_jobs=1) is None
    await database.finish_job(first.job_id, status="succeeded", result={})
    claimed_after_finish = await database.claim_next_job(owner="runner-2", max_running_jobs=1)
    assert claimed_after_finish is not None
    assert claimed_after_finish.job_id == second.job_id
    await database.close()
