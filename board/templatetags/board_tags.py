from django import template
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
