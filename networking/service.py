"""Hotspot state for the portal. Web requests never execute network commands."""
from django.conf import settings
from django.db.models import F
from django.utils import timezone
from portal.models import HotspotState, PortalSettings

ADDRESS = '10.0.0.1'
PORTAL_URL = 'http://10.0.0.1/'
APPLY_DELAY = 8


class HotspotError(ValueError):
    pass


def request_apply():
    HotspotState.load()
    HotspotState.objects.filter(pk=1).update(revision=F('revision') + 1, changed_at=timezone.now(), status='pending', error='')


def status(profile=None):
    profile = profile or PortalSettings.load()
    state = HotspotState.load()
    enabled = settings.HOTSPOT_ENABLED
    alive = enabled and state.alive
    current = enabled and alive and state.status == 'active' and state.revision == state.applied_revision
    return {'enabled': enabled, 'alive': alive, 'active': current,
            'status': state.status if alive else ('stopped' if enabled else 'disabled'),
            'label': 'Broadcasting' if current else ('Applying' if alive and state.status=='applying' else
                     'Pending' if alive and state.status=='pending' else
                     'Unavailable' if alive and state.status=='error' else
                     'Service offline' if enabled else 'Pi setup required'),
            'address': ADDRESS, 'url': PORTAL_URL, 'ssid': profile.wifi_ap_name,
            'applied_ssid': state.applied_ssid if alive else '',
            'protected': profile.ap_password_enabled,
            'error': state.error if enabled else '', 'apply_delay': APPLY_DELAY}
