import secrets
import uuid
import zoneinfo

from django.conf import settings
from django.db import models
from django.utils import timezone

from . import crypto


def new_token():
    return secrets.token_urlsafe(24)


class Settings(models.Model):
    """The one row of organization settings (pk=1). Read with `Settings.load()`."""

    org_name = models.CharField(max_length=120, default='')
    time_zone = models.CharField(max_length=64, default='UTC')
    keep_days = models.PositiveIntegerField(default=30)
    # The address people last used to reach Switchboard: links in failure alerts start with it.
    public_url = models.CharField(max_length=300, blank=True, default='')

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        row, _ = cls.objects.get_or_create(pk=1)
        return row

    def zone(self):
        try:
            return zoneinfo.ZoneInfo(self.time_zone)
        except Exception:
            return zoneinfo.ZoneInfo('UTC')


class Secret(models.Model):
    """A named value (a bot token, an API key) HTTP steps use as {{secret.NAME}}.

    Stored encrypted and never shown again after it is entered.
    """

    name = models.SlugField(max_length=60, unique=True)
    value_enc = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def set_value(self, value):
        self.value_enc = crypto.encrypt(value)

    @property
    def value(self):
        return crypto.decrypt(self.value_enc)


class Folder(models.Model):
    TINTS = ['indigo', 'green', 'pink', 'violet', 'amber', 'sky']

    name = models.CharField(max_length=80)
    tint = models.CharField(max_length=12, default='violet')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']

    def __str__(self):
        return self.name


class Connection(models.Model):
    """Our authorised link to one building (docs/PROTOCOL.md).

    `pending` rows are handshakes that haven't come back yet; `broken` ones had their key
    refused, so their nodes stay visible but calls fail until someone reconnects.
    """

    PENDING, CONNECTED, BROKEN = 'pending', 'connected', 'broken'

    building = models.CharField(max_length=60, blank=True, default='')   # the product's slug
    name = models.CharField(max_length=80, blank=True, default='')       # the product's name
    instance = models.CharField(max_length=160, blank=True, default='')  # the organization there
    base_url = models.CharField(max_length=300)
    api_base = models.CharField(max_length=300, blank=True, default='')
    manifest = models.JSONField(default=dict)
    manifest_at = models.DateTimeField(null=True, blank=True)
    api_key_enc = models.TextField(blank=True, default='')
    webhook_secret_enc = models.TextField(blank=True, default='')
    remote_id = models.CharField(max_length=80, blank=True, default='')
    hook_token = models.CharField(max_length=64, unique=True, default=new_token)
    state = models.CharField(max_length=64, blank=True, default='')
    status = models.CharField(max_length=12, default=PENDING)
    last_error = models.CharField(max_length=300, blank=True, default='')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    connected_at = models.DateTimeField(null=True, blank=True)
    last_event_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['name', 'id']

    def __str__(self):
        return f'{self.name} ({self.instance})' if self.instance else self.name

    @property
    def api_key(self):
        return crypto.decrypt(self.api_key_enc)

    @property
    def webhook_secret(self):
        return crypto.decrypt(self.webhook_secret_enc)

    @property
    def host(self):
        return self.base_url.split('://', 1)[-1]

    def triggers(self):
        return [t for t in self.manifest.get('triggers', []) if isinstance(t, dict) and t.get('type')]

    def actions(self):
        return [a for a in self.manifest.get('actions', []) if isinstance(a, dict) and a.get('name')]

    def trigger(self, type_):
        return next((t for t in self.triggers() if t['type'] == type_), None)

    def action(self, name):
        return next((a for a in self.actions() if a['name'] == name), None)


class Workflow(models.Model):
    """A trigger followed by a short chain of steps, kept as JSON in `steps`.

    Each step is {"uid", "node", "connection", "key", "params"}; the first one is the
    trigger. `board/nodes.py` says what every kind of node takes and gives.
    """

    name = models.CharField(max_length=120, default='Untitled workflow')
    description = models.CharField(max_length=300, blank=True, default='')
    folder = models.ForeignKey(Folder, null=True, blank=True, on_delete=models.SET_NULL, related_name='workflows')
    enabled = models.BooleanField(default=False)
    paused_at = models.DateTimeField(null=True, blank=True)
    steps = models.JSONField(default=list)
    # The last output each step gave in a test, by uid: the sample data the editor shows.
    samples = models.JSONField(default=dict)
    hook_token = models.CharField(max_length=64, unique=True, default=new_token)
    next_fire_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at', 'id']

    def __str__(self):
        return self.name

    @property
    def trigger(self):
        return self.steps[0] if self.steps else None


class Event(models.Model):
    """One event a building sent us. Kept `Settings.keep_days` so duplicates are dropped and
    calls that came in while a workflow was paused can be replayed."""

    connection = models.ForeignKey(Connection, on_delete=models.CASCADE, related_name='events')
    event_id = models.CharField(max_length=120)
    type = models.CharField(max_length=120, db_index=True)
    payload = models.JSONField(default=dict)
    summary = models.CharField(max_length=200, blank=True, default='')
    received_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['connection', 'event_id'], name='one_event_per_connection')]
        ordering = ['-received_at', '-id']


def new_dedupe():
    return uuid.uuid4().hex


class Run(models.Model):
    QUEUED, RUNNING, SUCCEEDED, FAILED, STOPPED = 'queued', 'running', 'succeeded', 'failed', 'stopped'
    DONE = (SUCCEEDED, FAILED, STOPPED)

    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name='runs')
    status = models.CharField(max_length=12, default=QUEUED, db_index=True)
    trigger_label = models.CharField(max_length=200, blank=True, default='')
    event = models.ForeignKey(Event, null=True, blank=True, on_delete=models.SET_NULL, related_name='runs')
    is_test = models.BooleanField(default=False)
    # Failures of a workflow that itself reports failures don't start it again.
    from_failure = models.BooleanField(default=False)
    # The workflow's steps as they were when the run started, so a retry repeats the same thing.
    steps = models.JSONField(default=list)
    # What each finished step gave, by uid. The trigger's entry is there from the start.
    context = models.JSONField(default=dict)
    # With the step's uid this makes the Idempotency-Key, so a retry never does an action twice.
    dedupe = models.CharField(max_length=140, default=new_dedupe)
    start_at = models.PositiveIntegerField(default=0)
    failed_step = models.PositiveIntegerField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True, default='')
    retries = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['workflow', 'event'], condition=models.Q(event__isnull=False, is_test=False),
                name='one_run_per_event',
            ),
        ]


class RunStep(models.Model):
    OK, FAILED, STOPPED, SKIPPED = 'ok', 'failed', 'stopped', 'skipped'

    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name='step_results')
    index = models.PositiveIntegerField()
    uid = models.CharField(max_length=24)
    title = models.CharField(max_length=160)
    status = models.CharField(max_length=10)
    note = models.CharField(max_length=300, blank=True, default='')
    sent = models.JSONField(null=True, blank=True)
    output = models.JSONField(null=True, blank=True)
    error = models.CharField(max_length=500, blank=True, default='')
    tries = models.PositiveIntegerField(default=1)
    duration_ms = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['index']
        constraints = [models.UniqueConstraint(fields=['run', 'index'], name='one_result_per_step')]
