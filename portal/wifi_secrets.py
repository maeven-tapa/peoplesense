"""AP credentials are encrypted; no plaintext is returned to portal clients."""
import os
import tempfile
from pathlib import Path
from cryptography.fernet import Fernet
from django.conf import settings


def cipher():
    key = settings.AP_SECRET_KEY
    if not key:
        path = Path(settings.AP_KEY_FILE)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                # Publish a complete key atomically without replacing a key
                # another request created. Temporary files have mode 0600.
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix='ap-key-', delete=False) as file:
                    temporary = Path(file.name)
                    file.write(Fernet.generate_key())
                    file.flush()
                    os.fsync(file.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
        key = path.read_bytes()
    return Fernet(key)


def encrypt_password(password):
    return cipher().encrypt(password.encode('ascii')).decode('ascii')


def device_ap_password(profile):
    """For the local hotspot adapter only; never expose through a view."""
    if not profile.ap_password_enabled or not profile.wifi_ap_password_encrypted:
        return ''
    return cipher().decrypt(profile.wifi_ap_password_encrypted.encode('ascii')).decode('ascii')
