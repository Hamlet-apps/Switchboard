from django.contrib.auth import get_user_model
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

from . import worker

OPEN_PREFIXES = ('/hooks/', '/static/', '/healthz')
_has_users = False


class SetupMiddleware:
    """While nobody has an account, every page leads to first-time setup."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        global _has_users
        if not _has_users and not request.path.startswith(OPEN_PREFIXES):
            # Only "there are users" is remembered: it never becomes false again while running.
            _has_users = get_user_model().objects.exists()
            if not _has_users and request.path != reverse('setup'):
                return redirect('setup')
        return self.get_response(request)


class WorkerMiddleware:
    """Starts the background worker with the first request, so queued runs and schedules
    carry on after a restart without waiting for the next event."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.started = False

    def __call__(self, request):
        if not self.started:
            self.started = True
            worker.kick()
        return self.get_response(request)


class ZoneMiddleware:
    """Pages show times on the organization's clock (Settings > Organization)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(OPEN_PREFIXES):
            return self.get_response(request)
        from .models import Settings
        timezone.activate(Settings.load().zone())
        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()
