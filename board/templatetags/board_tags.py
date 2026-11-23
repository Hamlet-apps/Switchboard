import os

from django import template
from django.conf import settings
from django.contrib.staticfiles import finders
from django.templatetags.static import static
from django.utils import timezone

register = template.Library()


@register.filter
def ago(when):
    """"just now", "14 min ago", "2h ago", "Yesterday", "3 days ago"; "Never" for nothing."""
    if not when:
        return 'Never'
    seconds = (timezone.now() - when).total_seconds()
    if seconds < 60:
        return 'Just now'
    if seconds < 3600:
        return f'{int(seconds // 60)} min ago'
    if seconds < 86400:
        return f'{int(seconds // 3600)}h ago'
    days = int(seconds // 86400)
    return 'Yesterday' if days == 1 else f'{days} days ago'


@register.simple_tag
def asset(path):
    """`{% static %}` that a browser can't keep an old copy of.

    In production the file names already carry a hash. In development the address gets the
    file's modified time, so an edited stylesheet or script is fetched again on the next load.
    """
    url = static(path)
    if settings.DEBUG:
        found = finders.find(path)
        if found:
            url += f'?v={int(os.path.getmtime(found))}'
    return url
