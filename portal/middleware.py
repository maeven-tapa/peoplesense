from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from django.conf import settings
from django.utils import timezone
from django.http import HttpResponse
from django.shortcuts import redirect
from .models import PortalSettings


class ApplianceAccessMiddleware:
    def __init__(self,get_response):
        self.get_response=get_response
    def __call__(self,request):
        profile=PortalSettings.load()
        try:zone=ZoneInfo(profile.time_zone)
        except (ZoneInfoNotFoundError,ValueError):zone=ZoneInfo(settings.TIME_ZONE)
        with timezone.override(zone):
            return self.authorize(request,profile)
    def authorize(self,request,profile):
        path=request.path
        public=path in ['/','/login/','/logout/'] or path.startswith('/static/')
        if not public:
            if not request.user.is_authenticated:
                return redirect('login')
            if not request.user.is_staff:
                return HttpResponse('Administrator access required.',status=403)
            if profile.setup_stage<4 and not request.session.get('setup_deferred') and not path.startswith('/setup/'):
                line_controls=False
                if profile.setup_stage==3 and profile.active_device_id:
                    prefix=f'/devices/{profile.active_device_id}/'
                    line_controls=path.startswith(prefix+'camera/') or path in [prefix+'tripwire/',prefix+'mode/']
                if not line_controls:
                    return redirect('setup')
        return self.get_response(request)
