import csv
import io
import tempfile
from datetime import datetime, timezone as utc
from pathlib import Path
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from portal.models import PortalSettings, Device, Detection
from portal.wifi_secrets import device_ap_password


class DeviceSettingsTests(TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        override=override_settings(AP_KEY_FILE=Path(folder.name)/'wifi.key',AP_SECRET_KEY=None)
        override.enable();self.addCleanup(override.disable)
        self.user=get_user_model().objects.create_user(username='settings-admin',password='Existing-password-42!',is_staff=True)
        self.camera=Device.objects.create(name='Single camera',location='Door')
        self.profile=PortalSettings.load();self.profile.active_device=self.camera;self.profile.setup_stage=4;self.profile.save()
        self.client.force_login(self.user)

    def wifi(self,**values):
        return self.client.post('/settings/',{'action':'wifi','wifi_ap_name':'Entrance AP',**values})

    def test_settings_has_independent_cards_and_no_building_controls(self):
        response=self.client.get('/settings/')
        for text in ['Device configuration','Change administrator password','Time & region','Save Wi-Fi settings','Save time zone','Current password']:
            self.assertContains(response,text)
        for text in ['Building configuration','name="building_name"','name="capacity"','name="temperature_threshold"']:
            self.assertNotContains(response,text)
        self.assertIn('no-store',response['Cache-Control'])

    def test_wifi_password_is_encrypted_hidden_and_retained_when_blank(self):
        password='Wifi-Secret-42!'
        self.assertRedirects(self.wifi(ap_password_enabled='on',wifi_ap_password=password),'/settings/')
        self.profile.refresh_from_db()
        token=self.profile.wifi_ap_password_encrypted
        self.assertTrue(self.profile.ap_password_enabled)
        self.assertNotIn(password,token)
        self.assertEqual(device_ap_password(self.profile),password)
        response=self.client.get('/settings/')
        self.assertNotContains(response,password);self.assertNotContains(response,token)
        self.assertRedirects(self.wifi(ap_password_enabled='on',wifi_ap_password='',wifi_ap_name='Renamed AP'),'/settings/')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.wifi_ap_password_encrypted,token)
        self.assertEqual(self.profile.wifi_ap_name,'Renamed AP')
        self.assertNotIn(password,self.client.get('/api/dashboard/').content.decode())

    def test_wifi_password_validation_does_not_save_partial_changes(self):
        for password in ['', 'short', 'a'*64, 'abcdefgh\n', 'Password🙂']:
            response=self.wifi(ap_password_enabled='on',wifi_ap_password=password)
            self.assertEqual(response.status_code,200)
            self.assertTrue(response.context['wifi_form'].errors)
            self.profile.refresh_from_db()
            self.assertEqual(self.profile.wifi_ap_name,'PeopleSense-AP')
            self.assertFalse(self.profile.ap_password_enabled)
        self.assertEqual(self.wifi(wifi_ap_name='a'*33).status_code,200)

    def test_disabling_ap_protection_removes_saved_secret(self):
        self.wifi(ap_password_enabled='on',wifi_ap_password='Wifi-Secret-42!')
        self.wifi(wifi_ap_password='Ignored-Secret-42!')
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.ap_password_enabled)
        self.assertEqual(self.profile.wifi_ap_password_encrypted,'')
        self.assertEqual(device_ap_password(self.profile),'')

    def test_unavailable_encryption_key_requires_reentry(self):
        self.wifi(ap_password_enabled='on',wifi_ap_password='Wifi-Secret-42!')
        from cryptography.fernet import Fernet
        with override_settings(AP_SECRET_KEY=Fernet.generate_key()):
            response=self.wifi(ap_password_enabled='on',wifi_ap_password='')
            self.assertContains(response,'The saved password is unavailable.')
            self.assertRedirects(self.wifi(ap_password_enabled='on',wifi_ap_password='Replacement-Wifi-42!'),'/settings/')
            self.profile.refresh_from_db()
            self.assertEqual(device_ap_password(self.profile),'Replacement-Wifi-42!')

    @patch('portal.views.timezone.now',return_value=datetime(2026,10,7,12,tzinfo=utc.utc))
    def test_time_zone_applies_to_page_api_and_csv_without_changing_events(self,clock):
        stamp=datetime(2026,10,6,23,30,tzinfo=utc.utc)
        event=Detection.objects.create(device=self.camera,direction='in',timestamp=stamp)
        original=timezone.get_current_timezone_name()
        self.assertRedirects(self.client.post('/settings/',{'action':'timezone','time_zone':'America/New_York'}),'/settings/')
        self.assertContains(self.client.get('/activity/'),'America/New_York')
        self.assertEqual(self.client.get('/api/dashboard/').json()['time_zone'],'America/New_York')
        rows=list(csv.reader(io.StringIO(self.client.get('/reports/export/',{'period':'30d'}).content.decode())))
        self.assertEqual(rows[0][1],'Timestamp (America/New_York)')
        self.assertEqual(rows[1][1],'2026-10-06T19:30:00-04:00')
        event.refresh_from_db();self.assertEqual(event.timestamp,stamp)
        self.assertEqual(timezone.get_current_timezone_name(),original)
        response=self.client.post('/settings/',{'action':'timezone','time_zone':'Invalid/Zone'})
        self.assertEqual(response.status_code,200);self.assertTrue(response.context['timezone_form'].errors)
        self.profile.refresh_from_db();self.assertEqual(self.profile.time_zone,'America/New_York')

    def test_unknown_action_cannot_restore_removed_building_settings(self):
        response=self.client.post('/settings/',{'building_name':'Changed','capacity':3,'temperature_threshold':40})
        self.assertEqual(response.status_code,400)
        self.profile.refresh_from_db();self.assertEqual(self.profile.capacity,500)

    def test_admin_password_requires_current_and_confirmation_and_preserves_session(self):
        for old,new,confirm in [('wrong','New-password-42!','New-password-42!'),('Existing-password-42!','New-password-42!','Mismatch'),('Existing-password-42!','12345678','12345678')]:
            response=self.client.post('/settings/',{'action':'password','old_password':old,'new_password1':new,'new_password2':confirm})
            self.assertTrue(response.context['password_form'].errors)
            self.user.refresh_from_db();self.assertTrue(self.user.check_password('Existing-password-42!'))
            self.assertNotContains(response,'value="'+new+'"')
        self.assertRedirects(self.client.post('/settings/',{'action':'password','old_password':'Existing-password-42!','new_password1':'New-password-42!','new_password2':'New-password-42!'}),'/settings/')
        self.user.refresh_from_db();self.assertTrue(self.user.check_password('New-password-42!'))
        self.assertEqual(self.client.get('/dashboard/').status_code,200)
        self.assertFalse(Client().login(username='settings-admin',password='Existing-password-42!'))

    def test_password_card_completes_initial_security_without_resetting_setup(self):
        self.profile.setup_stage=0;self.profile.save()
        session=self.client.session;session['setup_deferred']=True;session.save()
        self.client.post('/settings/',{'action':'password','old_password':'Existing-password-42!','new_password1':'New-password-42!','new_password2':'New-password-42!'})
        self.profile.refresh_from_db();self.assertEqual(self.profile.setup_stage,1)
        self.assertTrue(self.client.session['setup_deferred'])

    def test_cards_require_admin_login_and_csrf(self):
        self.assertEqual(Client().get('/settings/').status_code,302)
        guest=get_user_model().objects.create_user(username='guest')
        client=Client();client.force_login(guest);self.assertEqual(client.get('/settings/').status_code,403)
        client=Client(enforce_csrf_checks=True);client.force_login(self.user)
        for action in ['wifi','password','timezone']:
            self.assertEqual(client.post('/settings/',{'action':action}).status_code,403)

    def test_quick_setup_uses_same_protected_ap_configuration(self):
        self.profile.setup_stage=1;self.profile.save()
        response=self.client.post('/setup/network/',{'wifi_ap_name':'Setup AP','ap_password_enabled':'on','wifi_ap_password':'Setup-Wifi-42!'})
        self.assertRedirects(response,'/setup/camera/',fetch_redirect_response=False)
        self.profile.refresh_from_db();self.assertEqual(device_ap_password(self.profile),'Setup-Wifi-42!')
