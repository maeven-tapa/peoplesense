from django.conf import settings
from django.utils import timezone
from django.db.models import Sum
from .models import Alert, Device, PortalSettings
from . import appliance

def portal_context(request):
    if not request.user.is_authenticated:
        return {}
    devices = list(appliance.cameras())
    context = {'portal_time_zone':timezone.get_current_timezone_name(), 'demo_mode': settings.DEMO_MODE, 'building': PortalSettings.load(),
            'alert_count': appliance.alerts().filter(acknowledged=False).count(),
            'appliance_camera':devices[0] if devices else None,
            'portal_ready':PortalSettings.load().setup_stage==4 or bool(request.session.get('setup_deferred')),
            'total_devices': len(devices), 'online_devices': sum(d.effective_status == 'online' for d in devices)}
    if request.path.startswith('/settings/') or request.path.startswith('/setup/'):
        from networking.service import status
        context['hotspot_state'] = status(context['building'])
    if request.path.startswith('/settings/'):
        context['device_entries'] = devices[0].detections.filter(direction='in').aggregate(total=Sum('count'))['total'] or 0 if devices else 0
    return context
