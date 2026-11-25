"""The portal owns one configured local camera; old records stay archived."""
from .models import Device, Detection, Alert, PortalSettings


def camera():
    profile = PortalSettings.load()
    if profile.active_device_id:
        return profile.active_device
    return None


def cameras():
    return Device.objects.filter(pk=PortalSettings.load().active_device_id)


def events():
    return Detection.objects.filter(device_id=PortalSettings.load().active_device_id)


def alerts():
    return Alert.objects.filter(device_id=PortalSettings.load().active_device_id)
