from django.contrib import admin
from .models import Alert, Detection, Device, PortalSettings

@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ['name', 'location', 'status', 'last_seen']
    exclude = ['api_key_hash']
@admin.register(Detection)
class DetectionAdmin(admin.ModelAdmin):
    list_display = ['device', 'direction', 'count', 'temperature', 'timestamp']
    list_filter = ['direction', 'device']
admin.site.register([Alert, PortalSettings])
