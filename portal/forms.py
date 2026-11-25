from django import forms
import math
from zoneinfo import available_timezones
from django.contrib.auth.forms import PasswordChangeForm
from django.conf import settings
from django.db import transaction
from .models import Device, PortalSettings
from vision.cameras import configure_camera_field

class WifiSetupForm(forms.ModelForm):
    persisted_fields = ['wifi_ap_name', 'ap_password_enabled', 'wifi_ap_password_encrypted']
    wifi_ap_password = forms.CharField(label='Wi-Fi AP password', required=False, strip=False,
        widget=forms.PasswordInput(attrs={'autocomplete':'new-password', 'maxlength':63}),
        help_text='Use 8–63 printable ASCII characters. Leave blank to keep your saved password.')
    class Meta:
        model=PortalSettings
        fields=['wifi_ap_name','ap_password_enabled','wifi_ap_password']
        labels={'wifi_ap_name':'Wi-Fi AP name (SSID)', 'ap_password_enabled':'Protect this AP with a password'}
        widgets={'wifi_ap_name':forms.TextInput(attrs={'placeholder':'PeopleSense-AP','autocomplete':'off'})}
    def clean_wifi_ap_name(self):
        value=self.cleaned_data['wifi_ap_name'].strip()
        if not value or len(value.encode('utf-8'))>32 or any(ord(c)<32 or ord(c)==127 for c in value):
            raise forms.ValidationError('Use 1–32 UTF-8 bytes without control characters.')
        return value
    def clean(self):
        data=super().clean()
        password=data.get('wifi_ap_password','')
        if data.get('ap_password_enabled'):
            if password:
                if not 8<=len(password)<=63 or any(ord(char)<32 or ord(char)>126 for char in password):
                    self.add_error('wifi_ap_password','Use 8–63 printable ASCII characters.')
            elif not self.instance.wifi_ap_password_encrypted:
                self.add_error('wifi_ap_password','Enter a password to protect this AP.')
            else:
                from .wifi_secrets import device_ap_password
                from cryptography.fernet import InvalidToken
                try:device_ap_password(self.instance)
                except (InvalidToken,ValueError,OSError):
                    self.add_error('wifi_ap_password','The saved password is unavailable. Enter it again.')
        return data
    @transaction.atomic
    def save(self,commit=True):
        from .wifi_secrets import encrypt_password
        profile=super().save(commit=False)
        if not profile.ap_password_enabled:
            profile.wifi_ap_password_encrypted=''
        elif self.cleaned_data.get('wifi_ap_password'):
            profile.wifi_ap_password_encrypted=encrypt_password(self.cleaned_data['wifi_ap_password'])
        if commit:
            profile.save(update_fields=self.persisted_fields)
            if settings.HOTSPOT_ENABLED:
                from networking.service import request_apply
                request_apply()
        return profile

class CameraSelectionForm(forms.Form):
    camera_source = forms.TypedChoiceField(label='USB camera', coerce=str, choices=())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        configure_camera_field(self.fields['camera_source'])


class ApplianceCameraForm(forms.ModelForm):
    camera_source=forms.TypedChoiceField(label='USB camera',coerce=str,choices=())
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        configure_camera_field(self.fields['camera_source'])
    class Meta:
        model=Device
        fields=['name','location','camera_source','detection_model','confidence_threshold','process_fps']
        labels={'name':'Camera name','location':'Doorway / location','detection_model':'Person detection model','process_fps':'AI detection frames per second'}
    def clean_confidence_threshold(self):
        value=self.cleaned_data['confidence_threshold']
        if not math.isfinite(value):raise forms.ValidationError('Enter a finite confidence threshold.')
        return value
    def save(self,commit=True):
        self.instance.source_type='usb'
        self.instance.temperature_enabled=False
        return super().save(commit=commit)

class CameraModeForm(forms.ModelForm):
    preview_fps = forms.TypedChoiceField(label='Preview speed', coerce=int, required=False, choices=Device._meta.get_field('preview_fps').choices)
    def clean_preview_fps(self):
        return self.cleaned_data.get('preview_fps') or self.instance.preview_fps or 30

    class Meta:
        model = Device
        fields = ['camera_mode', 'capture_interval', 'preview_fps']
        labels = {'camera_mode': 'Camera mode', 'capture_interval': 'Time between captures (seconds)'}
        widgets = {'capture_interval': forms.NumberInput(attrs={'min': 1, 'max': 86400, 'step': 1, 'list': 'capture-intervals'})}

class TripwireForm(forms.ModelForm):
    class Meta:
        model = Device
        fields = ['line_ax', 'line_ay', 'line_bx', 'line_by', 'entry_side', 'line_tolerance', 'follow_camera']
        widgets = {name: forms.HiddenInput() for name in ['line_ax', 'line_ay', 'line_bx', 'line_by']}
    def clean(self):
        data = super().clean()
        coords = [data.get(name) for name in ['line_ax', 'line_ay', 'line_bx', 'line_by']]
        if data.get('line_tolerance') is not None and not math.isfinite(data['line_tolerance']):
            self.add_error('line_tolerance','Enter a finite tolerance.')
        if any(value is not None and not math.isfinite(value) for value in coords):
            raise forms.ValidationError('Line coordinates must be finite.')
        if all(value is not None for value in coords) and math.hypot(coords[2]-coords[0], coords[3]-coords[1]) < 0.05:
            raise forms.ValidationError('The counting line must cover at least 5% of the image.')
        return data

class TimeZoneForm(forms.ModelForm):
    time_zone=forms.ChoiceField(label='Time zone',choices=[(zone,zone.replace('_',' ')) for zone in sorted(available_timezones())])
    class Meta:
        model = PortalSettings
        fields = ['time_zone']

class AdministratorPasswordForm(PasswordChangeForm):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['old_password'].widget.attrs.pop('autofocus',None)
        for name,label in [('old_password','Current password'),('new_password1','New password'),('new_password2','Confirm new password')]:
            self.fields[name].label=label


class FactoryResetForm(forms.Form):
    reset_password = forms.CharField(label='Current administrator password', strip=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'current-password'}))
    reset_confirmation = forms.CharField(label='Type RESET to confirm', max_length=5,
        widget=forms.TextInput(attrs={'autocomplete': 'off', 'placeholder': 'RESET', 'spellcheck': 'false'}))

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_reset_password(self):
        password = self.cleaned_data['reset_password']
        # Read the current account, rather than trust an earlier request's object.
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.filter(pk=self.user.pk, is_staff=True, is_active=True).first()
        if not user or not user.check_password(password):
            raise forms.ValidationError('Enter your current administrator password.')
        return password

    def clean_reset_confirmation(self):
        if self.cleaned_data['reset_confirmation'] != 'RESET':
            raise forms.ValidationError('Type RESET exactly to erase this device.')
        return 'RESET'
