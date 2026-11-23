"""Running workflows.

`start_run` records a run with the trigger's data; `execute` follows the wiring from
the trigger (an If or Switch leaves by one of its outputs), writing a RunStep per step it does. A step that hits a network error or a 5xx is tried again after
`settings.STEP_RETRY_WAITS`; a 4xx fails at once. A failed run can be retried from the step
that failed (`retry`): earlier steps' results are kept, and every action call carries an
Idempotency-Key made from the run and the step, so nothing is ever done twice.
"""
import hashlib
import logging
import time

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import buildings, nodes, templating
from .buildings import BuildingError
from .models import Connection, Event, Run, RunStep, Secret, Settings, Workflow

log = logging.getLogger(__name__)


class Stop(Exception):
    """A check didn't pass: the run ends here, on purpose."""


def _connections():
    return {c.id: c for c in Connection.objects.all()}


def _secrets():
    return {s.name: s.value for s in Secret.objects.all()}


def _redact(value, secrets):
    hidden = [v for v in secrets.values() if v]
    if not hidden:
        return value
    if isinstance(value, str):
        for secret in hidden:
            value = value.replace(secret, '••••')
        return value
    if isinstance(value, dict):
        return {k: _redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, secrets) for v in value]
    return value


def idempotency_key(dedupe, uid):
    return 'sb_' + hashlib.sha256(f'{dedupe}:{uid}'.encode()).hexdigest()[:40]


def summarize(template, data):
    """"{person.name} reached level {level}" filled from an event's data."""
    import re
    return re.sub(r'\{([^{}]+)\}', lambda m: templating.text(templating.dig(data, m.group(1).strip())), template or '').strip()


# --- one step ------------------------------------------------------------------------------

def _settings_of(step, definition, ctx, secrets):
    """The step's settings, resolved and shaped. Raises BuildingError naming what's wrong."""
    stored = {**nodes.new_params(definition), **(step.get('params') or {})}
    plain = {p['key']: stored.get(p['key'], '') for p in definition['params'] if p.get('plain')}
    values = {}
    for param in definition['params']:
        if not nodes.visible(param, plain):
            continue
        value = templating.resolve(stored.get(param['key'], ''), ctx, secrets)
        try:
            value = templating.coerce(value, param.get('value_type') or param['type'])
        except ValueError as error:
            raise BuildingError(f'{param["label"]}: {error}.')
        if value is None:
            if param.get('required') and definition['node'] not in ('condition', 'if', 'switch'):
                raise BuildingError(f'{param["label"]} is empty. Check where this step gets it from.')
            continue
        values[param['key']] = value
    return values


def _headers(text):
    headers = {}
    for line in str(text or '').splitlines():
        if ':' in line:
            name, value = line.split(':', 1)
            if name.strip():
                headers[name.strip()] = value.strip()
    return headers


def perform(step, definition, ctx, connections, secrets, dedupe):
    """Do one non-trigger step. Returns (output, what was sent, a short note)."""
    if definition.get('missing'):
        raise BuildingError(definition['desc'])
    values = _settings_of(step, definition, ctx, secrets)
    node = definition['node']
    if node == 'condition':
        passed = templating.compare(values.get('left'), values.get('op', 'true'), values.get('right'))
        if not passed:
            raise Stop("Check didn't pass, so the workflow stopped here")
        return {'passed': True}, values, 'Check passed'
    if node == 'if':
        result = templating.compare(values.get('left'), values.get('op', 'true'), values.get('right'))
        return {'result': result, 'branch': 'true' if result else 'false'}, values, 'True' if result else 'False'
    if node == 'switch':
        on = values.get('on')
        for case in step.get('cases') or []:
            first = templating.resolve(case.get('value', ''), ctx, secrets)
            if case.get('op') == 'between':
                hit = (templating.compare(on, 'gte', first)
                       and templating.compare(on, 'lte', templating.resolve(case.get('value2', ''), ctx, secrets)))
            else:
                hit = templating.compare(on, case.get('op') or 'eq', first)
            if hit:
                label = nodes.port_label(step, case['id'])
                return {'matched': True, 'branch': case['id'], 'case': label}, values, label
        return {'matched': False, 'branch': 'default', 'case': 'Otherwise'}, values, 'Otherwise'
    if node == 'http':
        body = values.get('body')
        output = buildings.http_request(values.get('method', 'POST'), str(values.get('url', '')).strip(),
                                        _headers(values.get('headers')), body)
        return output, {k: v for k, v in values.items()}, f"{output['status']}"
    if node == 'action':
        connection = connections[step['connection']]
        action = connection.action(step['key'])
        key = '' if definition.get('lookup') else idempotency_key(dedupe, step['uid'])
        output = buildings.call_action(connection, action, values, key)
        return output, values, connection.name
    raise BuildingError("This step can't run here.")


def perform_with_retries(step, definition, ctx, connections, secrets, dedupe, waits=None):
    """`perform`, trying again on errors worth retrying. Returns (output, sent, note, tries)."""
    waits = settings.STEP_RETRY_WAITS if waits is None else waits
    tries = 0
    while True:
        tries += 1
        try:
            return (*perform(step, definition, ctx, connections, secrets, dedupe), tries)
        except BuildingError as error:
            if error.retry and tries <= len(waits):
                time.sleep(waits[tries - 1])
                continue
            if tries > 1:
                error.args = (f'{error.args[0]} (after {tries} tries)',)
            error.tries = tries
            raise


# --- runs ------------------------------------------------------------------------------------

def start_run(workflow, trigger_output, label, event=None, is_test=False, from_failure=False):
    """Record a queued run. Returns None when this event already started one for the workflow."""
    steps = nodes.upgrade(workflow.steps)
    if not steps:
        return None
    run = Run(
        workflow=workflow, trigger_label=label[:200], event=event, is_test=is_test, from_failure=from_failure,
        steps=steps, context={steps[0]['uid']: trigger_output},
    )
    if event is not None and not is_test:
        run.dedupe = f'{event.connection_id}:{event.event_id}:{workflow.id}'
    try:
        with transaction.atomic():
            run.save()
    except IntegrityError:
        return None
    return run


class _Halt(Exception):
    """Ends a run from anywhere in the step tree."""

    def __init__(self, status, error='', uid=''):
        self.status, self.error, self.uid = status, error, uid


def execute(run):
    """Work through a queued run. Safe to call from several workers: only one claims it.

    Steps that already have a result (the trigger, and everything before the failed step on
    a retry) are not done again; an If or Switch that already decided keeps its decision.
    A step is never done twice in one run, so a loop in the wiring just ends the run.
    """
    if not Run.objects.filter(pk=run.pk, status=Run.QUEUED).update(status=Run.RUNNING, started_at=timezone.now()):
        return None
    run.refresh_from_db()
    started = time.monotonic()
    connections, secrets = _connections(), _secrets()
    ctx = dict(run.context)
    done = set(run.step_results.filter(status=RunStep.OK).values_list('uid', flat=True))
    order = {'next': run.step_results.count()}

    def record(step, definition, **result):
        existing = RunStep.objects.filter(run=run, uid=step['uid']).first()
        if existing is None:
            index, order['next'] = order['next'], order['next'] + 1
        else:
            index = existing.index
        defaults = {'index': index, 'title': definition['title'], 'tries': 1, 'duration_ms': 0, 'sent': None,
                    'output': None, 'error': '', 'note': '', **result}
        RunStep.objects.update_or_create(run=run, uid=step['uid'], defaults=defaults)

    def follow(steps):
        """From the trigger, do a step, leave by one of its outputs, and go where that leads."""
        by_uid = {step['uid']: step for step in steps}
        step, visited = (steps[0] if steps else None), set()
        while step is not None and step['uid'] not in visited:
            uid = step['uid']
            visited.add(uid)
            definition = nodes.describe(step, connections)
            if uid in ctx and uid in done:
                output = ctx[uid]
            elif uid in ctx:            # the trigger: its data came with the run
                output = ctx[uid]
                record(step, definition, status=RunStep.OK, output=output, note=run.trigger_label)
            else:
                began = time.monotonic()
                took = lambda: int((time.monotonic() - began) * 1000)  # noqa: E731
                try:
                    output, sent, note, tries = perform_with_retries(step, definition, ctx, connections, secrets, run.dedupe)
                except Stop as stop:
                    record(step, definition, status=RunStep.STOPPED, note=str(stop), duration_ms=took())
                    raise _Halt(Run.STOPPED)
                except BuildingError as failure:
                    record(step, definition, status=RunStep.FAILED, error=_redact(str(failure), secrets)[:500],
                           tries=getattr(failure, 'tries', 1), duration_ms=took())
                    raise _Halt(Run.FAILED, str(failure), uid)
                except Exception as crash:  # a bug in Switchboard must not leave the run "running" forever
                    log.exception('Run %s crashed at step %s', run.pk, uid)
                    message = f'Switchboard hit an unexpected problem: {crash}'
                    record(step, definition, status=RunStep.FAILED, error=message[:500], duration_ms=took())
                    raise _Halt(Run.FAILED, message, uid)
                ctx[uid] = output
                record(step, definition, status=RunStep.OK, output=output, sent=_redact(sent, secrets), note=note,
                       tries=tries, duration_ms=took())
            port = output.get('branch') if step.get('node') in nodes.BRANCHING and isinstance(output, dict) else 'main'
            step = by_uid.get((step.get('next') or {}).get(port))

    status, error, failed_uid = Run.SUCCEEDED, '', ''
    try:
        follow(nodes.upgrade(run.steps))
    except _Halt as halt:
        status, error, failed_uid = halt.status, halt.error, halt.uid

    run.status, run.error, run.failed_uid = status, _redact(error, secrets)[:500], failed_uid
    run.context = ctx
    run.finished_at = timezone.now()
    run.duration_ms += int((time.monotonic() - started) * 1000)
    run.save(update_fields=['status', 'error', 'failed_uid', 'context', 'finished_at', 'duration_ms'])
    if status == Run.FAILED and not run.is_test and not run.from_failure:
        _announce_failure(run)
    return run


def retry(run):
    """Queue a failed run again. It carries on from the step that failed."""
    if run.status != Run.FAILED or not run.failed_uid:
        return False
    updated = Run.objects.filter(pk=run.pk, status=Run.FAILED).update(
        status=Run.QUEUED, error='', failed_uid='', retries=run.retries + 1, finished_at=None,
    )
    if updated:
        run.refresh_from_db()
    return bool(updated)


def _announce_failure(run):
    """Start every "A workflow failed" workflow, so people can be told."""
    from . import worker
    listeners = [w for w in Workflow.objects.filter(enabled=True).exclude(pk=run.workflow_id)
                 if w.steps and w.steps[0].get('node') == 'failure']
    if not listeners:
        return
    failed = nodes.find(run.steps, run.failed_uid)
    title = nodes.describe(failed, _connections())['title'] if failed else ''
    base = Settings.load().public_url.rstrip('/')
    data = {
        'workflow': {'name': run.workflow.name, 'id': run.workflow_id},
        'run': {'id': run.pk, 'url': f'{base}/runs/?run={run.pk}'},
        'step': title, 'error': run.error,
    }
    for workflow in listeners:
        started = start_run(workflow, data, f'{run.workflow.name} failed', from_failure=True)
        if started:
            worker.submit(started)


# --- triggers ---------------------------------------------------------------------------------

def event_label(connection, trigger, data):
    summary = summarize((trigger or {}).get('summary', ''), data)
    return f"{connection.name} · {summary or (trigger or {}).get('title') or 'event'}"


def receive_event(connection, envelope):
    """Store a building's event and start the workflows listening for it.

    Returns (event, runs); event is None for a duplicate.
    """
    from . import worker
    event_id = str(envelope.get('id') or '')[:120]
    type_ = str(envelope.get('type') or '')[:120]
    data = envelope.get('data') if isinstance(envelope.get('data'), dict) else {}
    trigger = connection.trigger(type_)
    try:
        with transaction.atomic():
            event = Event.objects.create(
                connection=connection, event_id=event_id, type=type_, payload=envelope,
                summary=event_label(connection, trigger, data)[:200],
            )
    except IntegrityError:
        return None, []
    Connection.objects.filter(pk=connection.pk).update(last_event_at=timezone.now())
    runs = []
    for workflow in listeners(connection, type_):
        run = start_run(workflow, data, event.summary, event=event)
        if run:
            runs.append(run)
            worker.submit(run)
    return event, runs


def listeners(connection, type_, enabled=True):
    return [w for w in Workflow.objects.filter(enabled=enabled) if listens(w, connection.id, type_)]


def listens(workflow, connection_id, type_):
    trigger = workflow.trigger
    return bool(trigger and trigger.get('node') == 'event' and trigger.get('connection') == connection_id
                and trigger.get('key') == type_)


def missed_events(workflow):
    """Events that came in for a paused workflow since it was paused, oldest first."""
    trigger = workflow.trigger
    if workflow.enabled or not trigger or trigger.get('node') != 'event' or not workflow.paused_at:
        return Event.objects.none()
    return (Event.objects.filter(connection_id=trigger.get('connection'), type=trigger.get('key'),
                                 received_at__gte=workflow.paused_at)
            .exclude(runs__workflow=workflow).order_by('received_at'))


def replay(workflow, events):
    from . import worker
    count = 0
    for event in events:
        data = event.payload.get('data') if isinstance(event.payload.get('data'), dict) else {}
        run = start_run(workflow, data, event.summary, event=event)
        if run:
            worker.submit(run)
            count += 1
    return count


def trigger_sample(workflow, step, definition):
    """Data to test with: the newest real thing this trigger saw, else the examples."""
    node = step.get('node')
    if node == 'event':
        event = Event.objects.filter(connection_id=step.get('connection'), type=step.get('key')).first()
        if event and isinstance(event.payload.get('data'), dict):
            return event.payload['data'], True
    if node == 'webhook':
        last = workflow.runs.filter(is_test=False).first() if workflow.pk else None
        if last and isinstance(last.context.get(step['uid']), dict):
            return last.context[step['uid']], True
    sample = nodes.example_output(definition)
    now = timezone.localtime(timezone.now(), Settings.load().zone()).isoformat(timespec='seconds')
    for key in ('started_at', 'fired_at', 'received_at'):
        if key in sample:
            sample[key] = now
    return sample, False
