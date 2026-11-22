from .models import Settings


def shell(request):
    """What every page's rail needs."""
    if not getattr(request, 'user', None) or not request.user.is_authenticated:
        return {}
    name = request.user.get_full_name() or request.user.get_username()
    return {'org': Settings.load(), 'me_name': name, 'me_initial': (name[:1] or '?').upper()}
