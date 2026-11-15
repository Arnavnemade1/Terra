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
    terminal = operations.workflow_view(e, model, True, True, 100)
    assert terminal['availability'] == 'failed'
    assert terminal['heartbeat_age_s'] is None
    assert terminal['stage'] == 'failed'


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


def test_visibility_filters_cannot_escape_the_application_queue():
    from fastapi import HTTPException
    query = operations.visibility_query('Running', 'DroneAgentWorkflow', "drone' OR TaskQueue = 'other")
    assert "TaskQueue = 'terra-science'" in query
    assert "WorkflowId STARTS_WITH 'drone'' OR TaskQueue = ''other'" in query
    with pytest.raises(HTTPException): operations.visibility_query('MadeUpStatus')
    with pytest.raises(HTTPException): operations.visibility_query(workflow_type='PrivateWorkflow')


def test_event_metadata_excludes_all_payload_and_failure_fields():
    from temporalio.api.enums.v1 import EventType
    from temporalio.api.history.v1 import HistoryEvent, ActivityTaskScheduledEventAttributes
    from temporalio.api.common.v1 import ActivityType, Payloads, Payload
    event = HistoryEvent(event_id=12, event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED,
        activity_task_scheduled_event_attributes=ActivityTaskScheduledEventAttributes(activity_id='round-3', activity_type=ActivityType(name='coordination_round'),
        input=Payloads(payloads=[Payload(data=b'private-provider-key-never-serialize')])))
    event.event_time.FromDatetime(datetime.now(timezone.utc))
    view = operations.history_event(event)
    assert view['id'] == 12
    assert view['type'] == 'ActivityTaskScheduled'
    assert view['activity_type'] == 'coordination_round'
    assert 'private-provider-key' not in json.dumps(view)
    assert 'input' not in view


@pytest.mark.asyncio
async def test_execution_pagination_carries_an_opaque_token_and_real_counts(monkeypatch):
    from terra import app
    seen = []
    class Iterator:
        current_page = [execution()]
        next_page_token = b'opaque-next-page'
        async def fetch_next_page(self): pass
    class Client:
        namespace = 'default'
        def list_workflows(self, query, **kwargs): seen.append((query,kwargs)); return Iterator()
        async def count_workflows(self, query, **kwargs):
            return SimpleNamespace(groups=[SimpleNamespace(group_values=['Running'], count=12)])
    async def client():return Client()
    monkeypatch.setattr(app, 'temporal_client', client)
    monkeypatch.setattr(operations, 'read_models', lambda:{})
    first = await operations.execution_page('', '', '', 20, '')
    assert len(first['executions']) == 1
    assert first['counts'] == {'Running':12}
    second = await operations.execution_page('Running', '', '', 20, first['next_page_token'])
    assert seen[-1][1]['next_page_token'] == b'opaque-next-page'
    assert "ExecutionStatus = 'Running'" in seen[-1][0]
    assert second['namespace'] == 'default'


@pytest.mark.asyncio
async def test_bad_page_token_does_not_touch_temporal():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        await operations.execution_page('', '', '', 20, '!invalid!')
    assert error.value.status_code == 422


def _history_description(queue='terra-science', kind='DroneAgentWorkflow'):
    from temporalio.api.workflowservice.v1 import DescribeWorkflowExecutionResponse
    raw = DescribeWorkflowExecutionResponse()
    raw.execution_config.task_queue.name = queue
    e = execution('drone-agent-D-01', kind)
    e.raw_description = raw
    return e


@pytest.mark.asyncio
@pytest.mark.parametrize('queue,kind',[('another-queue','DroneAgentWorkflow'),('terra-science','UnrelatedWorkflow')])
async def test_history_refuses_other_queues_or_workflow_types(monkeypatch, queue, kind):
    from terra import app
    from fastapi import HTTPException
    operations._history_cache.clear()
    async def describe(**kwargs):return _history_description(queue,kind)
    async def client():return SimpleNamespace(get_workflow_handle=lambda *a,**kw:SimpleNamespace(describe=describe))
    monkeypatch.setattr(app, 'temporal_client', client)
    with pytest.raises(HTTPException) as error:
        await operations.execution_history('drone-agent-D-01', 'run-1')
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_history_is_bounded_and_reuses_a_short_cache(monkeypatch):
    from terra import app
    from temporalio.api.workflowservice.v1 import GetWorkflowExecutionHistoryReverseResponse
    from temporalio.api.enums.v1 import EventType
    from temporalio.api.history.v1 import HistoryEvent
    operations._history_cache.clear()
    requests=[]
    async def describe(**kwargs):return _history_description()
    async def reverse(request, **kwargs):
        requests.append(request)
        events=[]
        for i in range(250):
            e=HistoryEvent(event_id=i+1,event_type=EventType.EVENT_TYPE_WORKFLOW_TASK_SCHEDULED)
            e.event_time.FromDatetime(datetime.now(timezone.utc));events.append(e)
        response=GetWorkflowExecutionHistoryReverseResponse()
        response.history.events.extend(reversed(events));return response
    async def client():return SimpleNamespace(namespace='default',get_workflow_handle=lambda *a,**kw:SimpleNamespace(describe=describe),workflow_service=SimpleNamespace(get_workflow_execution_history_reverse=reverse))
    monkeypatch.setattr(app, 'temporal_client', client)
    monkeypatch.setattr(operations,'read_models',lambda:{})
    first=await operations.execution_history('drone-agent-D-01','run-1')
    assert len(first['events']) == 200
    assert first['events'][0]['id'] == 51
    assert first['events'][-1]['id'] == 250
    assert first['limited']
    assert await operations.execution_history('drone-agent-D-01','run-1') is first
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_history_errors_do_not_disclose_private_temporal_failures(monkeypatch):
    from terra import app
    from fastapi import HTTPException
    operations._history_cache.clear()
    async def client():raise RuntimeError('private-upstream-token')
    monkeypatch.setattr(app, 'temporal_client', client)
    with pytest.raises(HTTPException) as error:
        await operations.execution_history('drone-agent-D-01','run-1')
    assert error.value.status_code == 503
    assert 'private-upstream-token' not in error.value.detail
