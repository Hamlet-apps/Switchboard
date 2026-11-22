"""The background worker: runs queued runs, fires schedules and prunes old logs.

One daemon thread per web process, started on the first request (`WorkerMiddleware`) and
woken whenever a run is queued. Everything it needs is in the database, and runs are claimed
with a conditional update, so several processes (or `manage.py runworker`) can share the work.
Tests set RUN_INLINE: `submit` then runs on the calling thread.
"""
import datetime
import logging
import threading

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

log = logging.getLogger(__name__)

TICK = 20           # seconds between looks for schedules that are due
STUCK = 15 * 60     # a run "running" this long lost its worker: queue it again
_wake = threading.Event()
_thread = None
_lock = threading.Lock()
_last_prune = None


def submit(run):
    """Hand a queued run to the worker."""
    from . import engine
    if settings.RUN_INLINE:
        engine.execute(run)
        return
    transaction.on_commit(kick)


def kick():
    ensure_started()
    _wake.set()


def ensure_started():
    global _thread
    if not settings.WORKER_ENABLED or (_thread and _thread.is_alive()):
        return
    with _lock:
        if _thread and _thread.is_alive():
            return
        _thread = threading.Thread(target=loop, name='switchboard-worker', daemon=True)
        _thread.start()


def loop(forever=True):
    while True:
        try:
            close_old_connections()
            work()
        except Exception:
            log.exception('The worker hit a problem and will try again')
        finally:
            close_old_connections()
        if not forever:
            return
        _wake.wait(TICK)
        _wake.clear()


def work():
    from . import engine
    from .models import Run
    fire_schedules()
    stale = timezone.now() - datetime.timedelta(seconds=STUCK)
    Run.objects.filter(status=Run.RUNNING, started_at__lt=stale).update(status=Run.QUEUED)
    while True:
        run = Run.objects.filter(status=Run.QUEUED).order_by('created_at', 'id').first()
        if run is None:
            break
        engine.execute(run)
    prune()


# --- schedules -------------------------------------------------------------------------------

WEEKDAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']


def next_fire(params, after, zone):
    """When a schedule trigger fires next, strictly after `after` (aware)."""
    repeat = params.get('repeat') or 'week'
    try:
        hour, minute = (int(part) for part in str(params.get('time') or '08:00').split(':')[:2])
        hour, minute = min(max(hour, 0), 23), min(max(minute, 0), 59)
    except ValueError:
        hour, minute = 8, 0
    local = after.astimezone(zone)
    if repeat == 'hour':
        candidate = local.replace(minute=minute, second=0, microsecond=0)
        if candidate <= local:
            candidate += datetime.timedelta(hours=1)
        return candidate
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if repeat == 'day':
        if candidate <= local:
            candidate += datetime.timedelta(days=1)
        return candidate
    weekday = WEEKDAYS.index(params.get('weekday')) if params.get('weekday') in WEEKDAYS else 0
    candidate += datetime.timedelta(days=(weekday - candidate.weekday()) % 7)
    if candidate <= local:
        candidate += datetime.timedelta(days=7)
    return candidate


def describe_schedule(params):
    from .nodes import WEEKDAYS as NAMES
    repeat, at = params.get('repeat') or 'week', params.get('time') or '08:00'
    if repeat == 'hour':
        return f"Every hour at :{str(at).split(':')[-1]}"
    if repeat == 'day':
        return f'Every day at {at}'
    return f"{dict(NAMES).get(params.get('weekday'), 'Monday')}s at {at}"


def plan(workflow, now=None):
    """Set `next_fire_at` from the workflow's trigger (None unless it's an enabled schedule)."""
    from .models import Settings
    trigger = workflow.trigger
    if workflow.enabled and trigger and trigger.get('node') == 'schedule':
        workflow.next_fire_at = next_fire(trigger.get('params') or {}, now or timezone.now(), Settings.load().zone())
    else:
        workflow.next_fire_at = None


def fire_schedules(now=None):
    from . import engine
    from .models import Settings, Workflow
    now = now or timezone.now()
    zone = Settings.load().zone()
    fired = []
    for workflow in Workflow.objects.filter(enabled=True, next_fire_at__lte=now):
        due = workflow.next_fire_at
        params = (workflow.trigger or {}).get('params') or {}
        upcoming = next_fire(params, now, zone) if (workflow.trigger or {}).get('node') == 'schedule' else None
        # Whoever moves next_fire_at on wins the firing; other workers see it's no longer due.
        if not Workflow.objects.filter(pk=workflow.pk, next_fire_at=due).update(next_fire_at=upcoming):
            continue
        if upcoming is None:
            continue
        run = engine.start_run(workflow, {'fired_at': due.astimezone(zone).isoformat(timespec='seconds')},
                               f'Schedule · {describe_schedule(params)}')
        if run:
            fired.append(run)
            if settings.RUN_INLINE:
                engine.execute(run)
    return fired


def prune(now=None):
    """Drop runs and events older than the organization keeps them. At most hourly."""
    global _last_prune
    from .models import Event, Run, Settings
    now = now or timezone.now()
    if _last_prune and (now - _last_prune).total_seconds() < 3600:
        return
    _last_prune = now
    cutoff = now - datetime.timedelta(days=max(Settings.load().keep_days, 1))
    Run.objects.filter(created_at__lt=cutoff).exclude(status__in=[Run.QUEUED, Run.RUNNING]).delete()
    Event.objects.filter(received_at__lt=cutoff).delete()
