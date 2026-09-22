from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from drawbridge.storage import Database, IdempotencyConflict, QueueFull, StorageError, create_request_hash


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
async def test_concurrent_enqueue_accepts_distinct_requests_without_transaction_conflicts(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()

    async def enqueue(number: int) -> str:
        payload = {"number": number}
        result = await database.enqueue_job(
            action="demo",
            app=f"demo-{number}",
            environment="staging",
            payload=payload,
            idempotency_key=f"idem-{number:04d}",
            request_hash=create_request_hash(payload),
            max_queued_jobs=50,
            max_queued_jobs_per_target=5,
        )
        return result.job_id

    job_ids = await asyncio.gather(*(enqueue(number) for number in range(10)))

    assert len(set(job_ids)) == 10
    await database.close()


@pytest.mark.asyncio
async def test_concurrent_enqueue_deduplicates_the_same_idempotency_key(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()
    payload = {"value": 1}

    async def enqueue() -> tuple[str, bool]:
        result = await database.enqueue_job(
            action="demo",
            app="demo",
            environment="staging",
            payload=payload,
            idempotency_key="idem-shared",
            request_hash=create_request_hash(payload),
            max_queued_jobs=50,
            max_queued_jobs_per_target=5,
        )
        return result.job_id, result.created

    results = await asyncio.gather(*(enqueue() for _ in range(10)))

    assert len({job_id for job_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    await database.close()


@pytest.mark.asyncio
async def test_concurrent_enqueue_reuses_one_job_for_the_same_plan(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()
    await database.save_plan(
        plan_id="plan-1",
        app="demo",
        environment="staging",
        payload={},
        expires_at=9999999999,
    )

    async def enqueue(number: int) -> tuple[str, bool]:
        payload = {"plan_id": "plan-1"}
        result = await database.enqueue_job(
            action="deploy",
            app="demo",
            environment="staging",
            payload=payload,
            idempotency_key=f"idem-plan-{number}",
            request_hash=create_request_hash(payload),
            max_queued_jobs=50,
            max_queued_jobs_per_target=5,
            plan_id="plan-1",
        )
        return result.job_id, result.created

    results = await asyncio.gather(*(enqueue(number) for number in range(10)))

    assert len({job_id for job_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    await database.close()


@pytest.mark.asyncio
async def test_concurrent_enqueue_enforces_global_capacity_exactly(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()

    async def enqueue(number: int) -> object:
        payload = {"number": number}
        return await database.enqueue_job(
            action="demo",
            app=f"demo-{number}",
            environment="staging",
            payload=payload,
            idempotency_key=f"idem-global-{number}",
            request_hash=create_request_hash(payload),
            max_queued_jobs=3,
            max_queued_jobs_per_target=5,
        )

    results = await asyncio.gather(*(enqueue(number) for number in range(10)), return_exceptions=True)

    assert sum(not isinstance(result, BaseException) for result in results) == 3
    assert sum(isinstance(result, QueueFull) for result in results) == 7
    await database.close()


@pytest.mark.asyncio
async def test_concurrent_enqueue_enforces_target_capacity_exactly(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()

    async def enqueue(number: int) -> object:
        payload = {"number": number}
        return await database.enqueue_job(
            action="demo",
            app="demo",
            environment="staging",
            payload=payload,
            idempotency_key=f"idem-target-{number}",
            request_hash=create_request_hash(payload),
            max_queued_jobs=50,
            max_queued_jobs_per_target=2,
        )

    results = await asyncio.gather(*(enqueue(number) for number in range(10)), return_exceptions=True)

    assert sum(not isinstance(result, BaseException) for result in results) == 2
    assert sum(isinstance(result, QueueFull) for result in results) == 8
    await database.close()


@pytest.mark.asyncio
async def test_failed_concurrent_enqueue_does_not_rollback_successful_request(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()

    async def enqueue(value: int) -> object:
        payload = {"value": value}
        return await database.enqueue_job(
            action="demo",
            app="demo",
            environment="staging",
            payload=payload,
            idempotency_key="idem-conflict",
            request_hash=create_request_hash(payload),
            max_queued_jobs=50,
            max_queued_jobs_per_target=5,
        )

    results = await asyncio.gather(enqueue(1), enqueue(2), return_exceptions=True)

    successful = [result for result in results if not isinstance(result, BaseException)]
    assert len(successful) == 1
    assert sum(isinstance(result, IdempotencyConflict) for result in results) == 1
    job = await database.get_job(successful[0].job_id)  # type: ignore[union-attr]
    assert job is not None
    assert job.status == "queued"
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


@pytest.mark.asyncio
async def test_two_database_instances_cannot_claim_the_same_job(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    first_database = Database(path)
    second_database = Database(path)
    await first_database.initialize()
    await second_database.initialize()
    queued = await first_database.enqueue_job(
        action="demo",
        app="demo",
        environment="staging",
        payload={},
        idempotency_key="idem-claim",
        request_hash=create_request_hash({}),
        max_queued_jobs=50,
        max_queued_jobs_per_target=5,
    )

    claims = await asyncio.gather(
        first_database.claim_next_job(owner="runner-1", max_running_jobs=1),
        second_database.claim_next_job(owner="runner-2", max_running_jobs=1),
    )

    claimed = [claim for claim in claims if claim is not None]
    assert len(claimed) == 1
    assert claimed[0].job_id == queued.job_id
    assert claimed[0].status == "running"
    await first_database.close()
    await second_database.close()


@pytest.mark.asyncio
async def test_concurrent_binding_updates_increment_every_version(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()

    versions = await asyncio.gather(
        *(database.save_binding("demo", "staging", {"update": number}, status="deployable") for number in range(10))
    )

    assert sorted(versions) == list(range(1, 11))
    binding = await database.get_binding("demo", "staging")
    assert binding is not None
    assert binding["version"] == 10
    await database.close()


@pytest.mark.asyncio
async def test_release_and_successful_job_are_committed_together(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()
    queued = await database.enqueue_job(
        action="deploy",
        app="demo",
        environment="staging",
        payload={},
        idempotency_key="idem-release",
        request_hash=create_request_hash({}),
        max_queued_jobs=50,
        max_queued_jobs_per_target=5,
    )
    claimed = await database.claim_next_job(owner="runner-1")
    assert claimed is not None

    await database.complete_job_with_release(
        job_id=queued.job_id,
        result={"release_id": "release-1", "status": "succeeded"},
        release_id="release-1",
        app="demo",
        environment="staging",
        release_payload={"source_sha": "a" * 40},
        event_type="release_succeeded",
    )

    job = await database.get_job(queued.job_id)
    release = await database.get_release("release-1")
    assert job is not None
    assert job.status == "succeeded"
    assert job.result == {"release_id": "release-1", "status": "succeeded"}
    assert release is not None
    assert release["source_sha"] == "a" * 40
    await database.close()


@pytest.mark.asyncio
async def test_release_is_rolled_back_when_job_cannot_be_completed(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()

    with pytest.raises(StorageError, match="running job"):
        await database.complete_job_with_release(
            job_id="missing-job",
            result={"release_id": "release-1", "status": "succeeded"},
            release_id="release-1",
            app="demo",
            environment="staging",
            release_payload={"source_sha": "a" * 40},
        )

    assert await database.get_release("release-1") is None
    await database.close()
