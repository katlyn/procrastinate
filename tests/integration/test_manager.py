import datetime

import psycopg2.errors
import pytest

from procrastinate import exceptions, jobs, manager

from .. import conftest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def pg_job_manager(aiopg_connector):
    return manager.JobManager(connector=aiopg_connector)


@pytest.fixture
def get_all(aiopg_connector):
    async def f(table, *fields):
        return await aiopg_connector.execute_query_all_async(
            f"SELECT {', '.join(fields)} FROM {table}"
        )

    return f


@pytest.mark.parametrize(
    "job_kwargs",
    [
        {"queue": "queue_a"},
        {"queue": "queue_a", "scheduled_at": conftest.aware_datetime(2000, 1, 1)},
    ],
)
async def test_fetch_job(pg_job_manager, job_factory, job_kwargs):
    # Add a first started job
    job = job_factory(id=2, **job_kwargs)
    await pg_job_manager.defer_job_async(job_factory(queue="queue_a"))
    await pg_job_manager.fetch_job(queues=None)

    # Now add the job we're testing
    await pg_job_manager.defer_job_async(job)

    assert await pg_job_manager.fetch_job(queues=["queue_a"]) == job


@pytest.mark.parametrize(
    "job_kwargs",
    [
        # We won't see this one because of the lock
        {"queue": "queue_a", "lock": "lock_1"},
        # We won't see this one because of the queue
        {"queue": "queue_b"},
        # We won't see this one because of the scheduled date
        {"queue": "queue_a", "scheduled_at": conftest.aware_datetime(2100, 1, 1)},
    ],
)
async def test_get_job_no_result(pg_job_manager, job_factory, job_kwargs):
    job = job_factory(**job_kwargs)

    # Add a first started job
    await pg_job_manager.defer_job_async(job_factory(lock="lock_1"))
    await pg_job_manager.fetch_job(queues=None)

    # Now add the job we're testing
    await pg_job_manager.defer_job_async(job)

    assert await pg_job_manager.fetch_job(queues=["queue_a"]) is None


async def test_get_stalled_jobs(get_all, pg_job_manager, aiopg_connector, job_factory):
    await pg_job_manager.defer_job_async(
        job_factory(queue="queue_a", task_name="task_1")
    )
    job_id = (await get_all("procrastinate_jobs", "id"))[0]["id"]

    # No started job
    assert await pg_job_manager.get_stalled_jobs(nb_seconds=3600) == []

    # We start a job and fake its `started` state in the database
    job = await pg_job_manager.fetch_job(queues=["queue_a"])
    await aiopg_connector.execute_query_async(
        "INSERT INTO procrastinate_events(job_id, type, at) VALUES "
        "(%(job_id)s, 'started', NOW() - INTERVAL '30 minutes')",
        job_id=job_id,
    )

    # Nb_seconds parameter
    assert await pg_job_manager.get_stalled_jobs(nb_seconds=3600) == []
    assert await pg_job_manager.get_stalled_jobs(nb_seconds=1800) == [job]

    # Queue parameter
    assert await pg_job_manager.get_stalled_jobs(nb_seconds=1800, queue="queue_a") == [
        job
    ]
    assert await pg_job_manager.get_stalled_jobs(nb_seconds=1800, queue="queue_b") == []
    # Task name parameter
    assert await pg_job_manager.get_stalled_jobs(
        nb_seconds=1800, task_name="task_1"
    ) == [job]
    assert (
        await pg_job_manager.get_stalled_jobs(nb_seconds=1800, task_name="task_2") == []
    )


async def test_delete_old_jobs_job_is_not_finished(
    get_all, pg_job_manager, aiopg_connector, job_factory
):
    await pg_job_manager.defer_job_async(job_factory(queue="queue_a"))

    # No started job
    await pg_job_manager.delete_old_jobs(nb_hours=0)
    assert len(await get_all("procrastinate_jobs", "id")) == 1

    # We start a job
    job = await pg_job_manager.fetch_job(queues=["queue_a"])
    # We back date the started event
    await aiopg_connector.execute_query_async(
        f"UPDATE procrastinate_events SET at=at - INTERVAL '2 hours'"
        f"WHERE job_id={job.id}"
    )

    # The job is not finished so it's not deleted
    await pg_job_manager.delete_old_jobs(nb_hours=0)
    assert len(await get_all("procrastinate_jobs", "id")) == 1


async def test_delete_old_jobs_multiple_jobs(
    get_all, pg_job_manager, aiopg_connector, job_factory
):
    await pg_job_manager.defer_job_async(job_factory(queue="queue_a"))
    await pg_job_manager.defer_job_async(job_factory(queue="queue_b"))

    # We start both jobs
    job_a = await pg_job_manager.fetch_job(queues=["queue_a"])
    job_b = await pg_job_manager.fetch_job(queues=["queue_b"])
    # We finish both jobs
    await pg_job_manager.finish_job(
        job_a, status=jobs.Status.SUCCEEDED, delete_job=False
    )
    await pg_job_manager.finish_job(
        job_b, status=jobs.Status.SUCCEEDED, delete_job=False
    )
    # We back date the events for job_a
    await aiopg_connector.execute_query_async(
        f"UPDATE procrastinate_events SET at=at - INTERVAL '2 hours'"
        f"WHERE job_id={job_a.id}"
    )

    # Only job_a is deleted
    await pg_job_manager.delete_old_jobs(nb_hours=2)
    rows = await get_all("procrastinate_jobs", "id")
    assert len(rows) == 1
    assert rows[0]["id"] == job_b.id


async def test_delete_old_job_filter_on_end_date(
    get_all, pg_job_manager, aiopg_connector, job_factory
):
    await pg_job_manager.defer_job_async(job_factory(queue="queue_a"))
    # We start the job
    job = await pg_job_manager.fetch_job(queues=["queue_a"])
    # We finish the job
    await pg_job_manager.finish_job(job, status=jobs.Status.SUCCEEDED, delete_job=False)
    # We back date only the start event
    await aiopg_connector.execute_query_async(
        f"UPDATE procrastinate_events SET at=at - INTERVAL '2 hours'"
        f"WHERE job_id={job.id} AND TYPE='started'"
    )

    # Job is not deleted since it finished recently
    await pg_job_manager.delete_old_jobs(nb_hours=2)
    rows = await get_all("procrastinate_jobs", "id")
    assert len(rows) == 1


@pytest.mark.parametrize(
    "status, nb_hours, queue, include_error, should_delete",
    [
        # nb_hours
        (jobs.Status.SUCCEEDED, 1, None, False, True),
        (jobs.Status.SUCCEEDED, 3, None, False, False),
        # queue
        (jobs.Status.SUCCEEDED, 1, "queue_a", False, True),
        (jobs.Status.SUCCEEDED, 3, "queue_a", False, False),
        (jobs.Status.SUCCEEDED, 1, "queue_b", False, False),
        (jobs.Status.SUCCEEDED, 1, "queue_b", False, False),
        # include_error
        (jobs.Status.FAILED, 1, None, False, False),
        (jobs.Status.FAILED, 1, None, True, True),
    ],
)
async def test_delete_old_jobs_parameters(
    get_all,
    pg_job_manager,
    aiopg_connector,
    status,
    nb_hours,
    queue,
    include_error,
    should_delete,
    job_factory,
):
    await pg_job_manager.defer_job_async(job_factory(queue="queue_a"))

    # We start a job
    job = await pg_job_manager.fetch_job(queues=["queue_a"])
    # We finish the job
    await pg_job_manager.finish_job(job, status=status, delete_job=False)
    # We back date its events
    await aiopg_connector.execute_query_async(
        f"UPDATE procrastinate_events SET at=at - INTERVAL '2 hours'"
        f"WHERE job_id={job.id}"
    )

    await pg_job_manager.delete_old_jobs(
        nb_hours=nb_hours, queue=queue, include_error=include_error
    )
    nb_jobs = len(await get_all("procrastinate_jobs", "id"))
    if should_delete:
        assert nb_jobs == 0
    else:
        assert nb_jobs == 1


async def test_finish_job(get_all, pg_job_manager, job_factory):
    await pg_job_manager.defer_job_async(job_factory(queue="queue_a"))
    job = await pg_job_manager.fetch_job(queues=["queue_a"])

    assert await get_all("procrastinate_jobs", "status") == [{"status": "doing"}]
    events = await get_all("procrastinate_events", "type", "at")
    events_started = list(filter(lambda e: e["type"] == "started", events))
    assert len(events_started) == 1
    started_at = events_started[0]["at"]
    assert started_at.date() == datetime.datetime.utcnow().date()
    assert await get_all("procrastinate_jobs", "attempts") == [{"attempts": 0}]

    await pg_job_manager.finish_job(
        job=job, status=jobs.Status.SUCCEEDED, delete_job=False
    )
    expected = [{"status": "succeeded", "attempts": 1}]
    assert await get_all("procrastinate_jobs", "status", "attempts") == expected


@pytest.mark.parametrize("delete_job", [False, True])
async def test_finish_job_status_error(
    get_all, pg_job_manager, job_factory, delete_job
):
    job = job_factory(queue="queue_a")
    await pg_job_manager.defer_job_async(job)

    job_rows = await get_all("procrastinate_jobs", "id", "status")
    assert len(job_rows) == 1
    assert job_rows[0]["status"] == "todo"

    job_id = job_rows[0]["id"]
    job = job.evolve(id=job_id)

    await pg_job_manager.finish_job(
        job=job, status=jobs.Status.FAILED, delete_job=False
    )
    job_rows = await get_all("procrastinate_jobs", "status", "attempts")
    assert job_rows == [{"status": "failed", "attempts": 0}]

    with pytest.raises(exceptions.ConnectorException) as excinfo:
        await pg_job_manager.finish_job(
            job=job, status=jobs.Status.FAILED, delete_job=delete_job
        )
    assert isinstance(excinfo.value.__cause__, psycopg2.errors.RaiseException)
    assert (
        f'Job with id {job_id} was not found or not in "doing" or "todo" status'
        in str(excinfo.value.__cause__)
    )


async def test_retry_job(get_all, pg_job_manager, job_factory):
    await pg_job_manager.defer_job_async(job_factory())
    job1 = await pg_job_manager.fetch_job(queues=None)
    await pg_job_manager.retry_job(job=job1, retry_at=datetime.datetime.utcnow())

    job2 = await pg_job_manager.fetch_job(queues=None)

    assert job2.id == job1.id
    assert job2.attempts == job1.attempts + 1


async def test_finish_job_bad_end_status(get_all, pg_job_manager, job_factory):
    job = job_factory(queue="queue_a")
    await pg_job_manager.defer_job_async(job)

    job_rows = await get_all("procrastinate_jobs", "id", "status")
    assert len(job_rows) == 1
    assert job_rows[0]["status"] == "todo"

    job_id = job_rows[0]["id"]
    job = job.evolve(id=job_id)

    with pytest.raises(exceptions.ConnectorException) as excinfo:
        await pg_job_manager.finish_job(
            job=job, status=jobs.Status.TODO, delete_job=False
        )
    assert isinstance(excinfo.value.__cause__, psycopg2.errors.RaiseException)
    assert 'End status should be either "succeeded" or "failed"' in str(
        excinfo.value.__cause__
    )


async def test_enum_synced(aiopg_connector):
    # If this test breaks, it means you've changed either the task_status PG enum
    # or the python procrastinate.jobs.Status Enum without updating the other.
    pg_enum_rows = await aiopg_connector.execute_query_all_async(
        """SELECT e.enumlabel FROM pg_enum e
               JOIN pg_type t ON e.enumtypid = t.oid WHERE t.typname = %(type_name)s""",
        type_name="procrastinate_job_status",
    )

    pg_values = {row["enumlabel"] for row in pg_enum_rows}
    python_values = {status.value for status in jobs.Status.__members__.values()}
    assert pg_values == python_values


async def test_defer_job(pg_job_manager, get_all, job_factory):
    queue = "marsupilami"
    job = job_factory(
        id=0,
        queue=queue,
        task_name="bob",
        lock="sher",
        queueing_lock="houba",
        task_kwargs={"a": 1, "b": 2},
    )
    pk = await pg_job_manager.defer_job_async(job=job)

    result = await get_all(
        "procrastinate_jobs",
        "id",
        "args",
        "status",
        "lock",
        "queueing_lock",
        "task_name",
    )
    assert result == [
        {
            "id": pk,
            "args": {"a": 1, "b": 2},
            "status": "todo",
            "lock": "sher",
            "queueing_lock": "houba",
            "task_name": "bob",
        }
    ]


async def test_defer_job_violate_queueing_lock(pg_job_manager, job_factory):
    await pg_job_manager.defer_job_async(
        job_factory(
            id=1,
            queue="queue_a",
            task_name="task_1",
            lock="lock_1",
            queueing_lock="queueing_lock",
            task_kwargs={"a": "b"},
        )
    )
    with pytest.raises(exceptions.AlreadyEnqueued) as excinfo:
        await pg_job_manager.defer_job_async(
            job_factory(
                id=2,
                queue="queue_a",
                task_name="task_2",
                lock="lock_2",
                queueing_lock="queueing_lock",
                task_kwargs={"c": "d"},
            )
        )
    cause = excinfo.value.__cause__
    assert isinstance(cause, exceptions.UniqueViolation)
    assert cause.constraint_name == "procrastinate_jobs_queueing_lock_idx"


async def test_check_connection(pg_job_manager):
    assert await pg_job_manager.check_connection() is True


@pytest.fixture
async def fixture_jobs(pg_job_manager, job_factory):
    j1 = job_factory(
        queue="q1",
        lock="lock1",
        queueing_lock="queueing_lock1",
        task_name="task_foo",
        task_kwargs={"key": "a"},
    )
    j1 = j1.evolve(id=await pg_job_manager.defer_job_async(job=j1))

    j2 = job_factory(
        queue="q1",
        lock="lock2",
        queueing_lock="queueing_lock2",
        task_name="task_bar",
        task_kwargs={"key": "b"},
    )
    j2 = j2.evolve(id=await pg_job_manager.defer_job_async(job=j2))
    await pg_job_manager.finish_job(job=j2, status=jobs.Status.FAILED, delete_job=False)

    j3 = job_factory(
        queue="q2",
        lock="lock3",
        queueing_lock="queueing_lock3",
        task_name="task_foo",
        task_kwargs={"key": "c"},
    )
    j3 = j3.evolve(id=await pg_job_manager.defer_job_async(job=j3))
    await pg_job_manager.finish_job(
        job=j3, status=jobs.Status.SUCCEEDED, delete_job=False
    )

    j4 = job_factory(
        queue="q3",
        lock="lock4",
        queueing_lock="queueing_lock4",
        task_name="task_bar",
        task_kwargs={"key": "d"},
    )
    j4 = j4.evolve(id=await pg_job_manager.defer_job_async(job=j4))
    await pg_job_manager.fetch_job(queues=["q3"])

    return [j1, j2, j3, j4]


async def test_list_jobs_dict(fixture_jobs, pg_job_manager):
    j1, *_ = fixture_jobs
    assert (await pg_job_manager.list_jobs_async())[0] == {
        "id": j1.id,
        "status": "todo",
        "queue": j1.queue,
        "task": j1.task_name,
        "lock": j1.lock,
        "queueing_lock": j1.queueing_lock,
        "args": j1.task_kwargs,
        "scheduled_at": j1.scheduled_at,
        "attempts": j1.attempts,
    }


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({}, [1, 2, 3, 4]),
        ({"id": 1}, [1]),
        ({"lock": "lock3"}, [3]),
        ({"queue": "q1", "task": "task_foo"}, [1]),
        ({"status": "failed"}, [2]),
        ({"queueing_lock": "queueing_lock2"}, [2]),
    ],
)
async def test_list_jobs(fixture_jobs, kwargs, expected, pg_job_manager):
    assert [e["id"] for e in await pg_job_manager.list_jobs_async(**kwargs)] == expected


async def test_list_queues_dict(fixture_jobs, pg_job_manager):
    assert (await pg_job_manager.list_queues_async())[0] == {
        "name": "q1",
        "jobs_count": 2,
        "todo": 1,
        "doing": 0,
        "succeeded": 0,
        "failed": 1,
    }


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({}, ["q1", "q2", "q3"]),
        ({"queue": "q2"}, ["q2"]),
        ({"task": "task_foo"}, ["q1", "q2"]),
        ({"status": "todo"}, ["q1"]),
        ({"lock": "lock2"}, ["q1"]),
    ],
)
async def test_list_queues(fixture_jobs, kwargs, expected, pg_job_manager):
    assert [
        e["name"] for e in await pg_job_manager.list_queues_async(**kwargs)
    ] == expected


async def test_list_tasks_dict(fixture_jobs, pg_job_manager):
    assert (await pg_job_manager.list_tasks_async())[0] == {
        "name": "task_bar",
        "jobs_count": 2,
        "todo": 0,
        "doing": 1,
        "succeeded": 0,
        "failed": 1,
    }


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({}, ["task_bar", "task_foo"]),
        ({"queue": "q2"}, ["task_foo"]),
        ({"task": "task_foo"}, ["task_foo"]),
        ({"status": "todo"}, ["task_foo"]),
        ({"lock": "lock2"}, ["task_bar"]),
    ],
)
async def test_list_tasks(fixture_jobs, pg_job_manager, kwargs, expected):
    assert [
        e["name"] for e in await pg_job_manager.list_tasks_async(**kwargs)
    ] == expected


async def test_retry_job_return_info(fixture_jobs, pg_job_manager):
    await pg_job_manager.retry_job_return_info_async(job_id=4)
    (job1,) = await pg_job_manager.list_jobs_async(id=4)
    assert job1["status"] == "todo"


async def test_cancel_job_return_info(fixture_jobs, pg_job_manager):
    await pg_job_manager.cancel_job_return_info_async(job_id=1)
    (job1,) = await pg_job_manager.list_jobs_async(id=1)
    assert job1["status"] == "failed"
