"""Visibility is bounded, honest about outages, and excludes raw Temporal payloads."""
import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from temporalio.api.workflowservice.v1 import DescribeTaskQueueResponse
from temporalio.api.taskqueue.v1 import PollerInfo
from temporalio.client import WorkflowExecutionStatus

from terra import operations


def execution(identifier='investigation-1', kind='InvestigationWorkflow', status=WorkflowExecutionStatus.RUNNING):
    return SimpleNamespace(id=identifier, run_id='run-1', workflow_type=kind, status=status,
                           start_time=datetime.now(timezone.utc), close_time=None)


def test_blocked_source_is_not_reported_as_working():
    row = operations.workflow_view(execution(), {'stage':'prepare','model_status':'blocked'}, True, True, 100)
    assert row['availability'] == 'blocked'
    assert operations.workflow_view(execution(), {}, True, False, 100)['availability'] == 'unavailable'
    proposal = operations.workflow_view(execution(kind='AreaWatchWorkflow'), {'stage':'proposal'}, True, True, 100)
    assert proposal['availability'] == 'blocked'


def test_idle_heartbeat_uses_agent_cadence_and_terminal_status_is_authoritative():
    e = execution(kind='DroneAgentWorkflow')
    model = {'stage':'idle','heartbeat_age_s':40,'next_s':20}
    assert operations.workflow_view(e, model, True, True, 100)['availability'] == 'waiting'
    assert operations.workflow_view(e, {**model,'heartbeat_age_s':80}, True, True, 100)['availability'] == 'stale'
    e.status = WorkflowExecutionStatus.FAILED
    assert operations.workflow_view(e, model, True, True, 100)['availability'] == 'failed'


@pytest.mark.asyncio
async def test_pollers_must_have_recent_access(monkeypatch):
    monkeypatch.setattr(operations.time, 'time', lambda: 1000)
    async def describe(request, **kwargs):
        assert kwargs['timeout'].total_seconds() == 2
        return DescribeTaskQueueResponse(pollers=[PollerInfo(identity='private-worker', last_access_time=Timestamp(seconds=990)),
                                                  PollerInfo(identity='dead-worker', last_access_time=Timestamp(seconds=800))])
    client = SimpleNamespace(namespace='default',workflow_service=SimpleNamespace(describe_task_queue=describe))
    assert await operations.queue_pollers(client, 1) == 1


@pytest.mark.asyncio
async def test_visibility_requests_are_coalesced_even_if_one_viewer_disconnects():
    monitor = operations.AgentMonitor()
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def refresh():
        calls.append(1); started.set(); await release.wait(); return {'ok':True}
    monitor.refresh = refresh
    first = asyncio.create_task(monitor.snapshot())
    await started.wait()
    second = asyncio.create_task(monitor.snapshot())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second == {'ok':True}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_snapshot_scopes_queue_and_never_returns_payloads(monkeypatch):
    from terra import app
    from temporalio.api.workflow.v1 import PendingActivityInfo
    from temporalio.api.common.v1 import ActivityType
    from temporalio.api.workflowservice.v1 import DescribeWorkflowExecutionResponse
    now = datetime.now(timezone.utc)
    queries = []
    class Client:
        namespace = 'default'
        def list_workflows(self, query, **kwargs):
            queries.append((query,kwargs))
            async def items():
                if "!= 'Running'" not in query:
                    yield execution()
                    yield execution('unrelated', 'OtherApplicationWorkflow')
            return items()
        def get_workflow_handle(self, identifier, **kwargs):
            async def describe(**kwargs):
                response = DescribeWorkflowExecutionResponse(pending_activities=[PendingActivityInfo(
                    activity_type=ActivityType(name='compute_science'), state=2, attempt=2)])
                response.pending_activities[0].last_failure.message = 'private-failure-secret'
                return SimpleNamespace(raw_description=response)
            return SimpleNamespace(describe=describe)
    async def client(): return Client()
    async def pollers(*args): return 1
    monkeypatch.setattr(app, 'temporal_client', client)
    monkeypatch.setattr(operations, 'queue_pollers', pollers)
    monkeypatch.setattr(operations, 'read_models', lambda: {'investigation-1':{'stage':'analyze','updated_at':now.isoformat()}})
    monkeypatch.setattr(operations, 'worker_ready', lambda _: True)
    monitor = operations.AgentMonitor()
    value = await monitor.snapshot()
    assert value['summary']['running'] == 1
    assert value['temporal']['worker_available']
    assert value['agents'][0]['activities'][0]['attempt'] == 2
    assert value['agents'][0]['details_live']
    assert 'private-failure-secret' not in json.dumps(value)
    assert all('TaskQueue =' in query for query,_ in queries)
    assert all(args['limit'] <= 200 for _,args in queries)
    assert await monitor.snapshot() is value
    assert len(queries) == 2
    async def offline(): raise RuntimeError('secret endpoint unavailable')
    monkeypatch.setattr(app, 'temporal_client', offline)
    monitor.at = 0
    stale = await monitor.snapshot()
    assert stale['stale']
    assert stale['agents'][0]['availability'] == 'unavailable'
    assert not stale['agents'][0]['details_live']
    assert 'secret endpoint' not in json.dumps(stale)
