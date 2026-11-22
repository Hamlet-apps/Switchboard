"""Encrypts what Switchboard has to keep and use again: building keys, webhook secrets and
the secrets HTTP steps use. The key is derived from SECRET_KEY, so changing SECRET_KEY means
reconnecting buildings and re-entering secrets."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    key = hashlib.sha256(('switchboard-secrets:' + settings.SECRET_KEY).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value):
    return _fernet().encrypt((value or '').encode()).decode() if value else ''


def decrypt(token):
    if not token:
        return ''
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ''
