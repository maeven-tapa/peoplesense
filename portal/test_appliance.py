import io
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from portal.models import Device, Detection, PortalSettings
from portal.forms import WifiSetupForm


class ApplianceSetupTests(TestCase):
    def setUp(self):
        discovery = patch('vision.cameras.connected_camera_choices', return_value=[('0', 'Webcam'), ('1', 'Second webcam')])
        discovery.start()
        self.addCleanup(discovery.stop)
        call_command('bootstrap_appliance',stdout=io.StringIO())
        self.profile=PortalSettings.load()
        self.camera=self.profile.active_device

    def login(self):
        return self.client.post('/',{'username':'admin','password':'12345678'})

    def security(self):
        return self.client.post('/setup/security/',{'new_password1':'Doorway-Portal-42!','new_password2':'Doorway-Portal-42!'})

    def test_home_is_login_and_default_credentials_start_setup(self):
        page=self.client.get('/')
        self.assertContains(page,'Manage your PeopleSense device')
        self.assertNotContains(page,'First-time login')
        self.assertRedirects(self.login(),'/setup/',fetch_redirect_response=False)
        self.assertRedirects(self.client.get('/dashboard/'),'/setup/',fetch_redirect_response=False)
        self.assertRedirects(self.client.get('/setup/line/'),'/setup/',fetch_redirect_response=False)

    def test_wrong_password_and_non_administrator_are_rejected(self):
        response=self.client.post('/',{'username':'admin','password':'wrong'})
        self.assertEqual(response.status_code,200)
        self.assertFalse(response.wsgi_request.user.is_authenticated)
        self.assertContains(response,'First-time login')
        self.assertContains(response,'12345678')
        get_user_model().objects.create_user(username='guest',password='Guest-42-password')
        self.assertContains(self.client.post('/',{'username':'guest','password':'Guest-42-password'}),'Use the device administrator account')

    def test_login_required_for_preview_controls_reports_and_dashboard(self):
        for path in ['/dashboard/','/devices/','/activity/','/alerts/','/settings/','/api/dashboard/',f'/devices/{self.camera.pk}/camera/frame/']:
            self.assertRedirects(self.client.get(path),'/',fetch_redirect_response=False)

    def test_new_password_required_and_session_survives(self):
        self.login()
        bad=self.client.post('/setup/security/',{'new_password1':'12345678','new_password2':'12345678'})
        self.assertEqual(bad.status_code,200)
        self.profile.refresh_from_db();self.assertEqual(self.profile.setup_stage,0)
        self.assertRedirects(self.security(),'/setup/network/',fetch_redirect_response=False)
        self.assertEqual(self.client.get('/setup/network/').status_code,200)
        self.assertTrue(get_user_model().objects.get(username='admin').check_password('Doorway-Portal-42!'))
        self.assertFalse(Client().login(username='admin',password='12345678'))

    def test_full_setup_saves_ap_camera_and_line_and_opens_dashboard(self):
        self.login();self.security()
        self.assertRedirects(self.client.post('/setup/network/',{'wifi_ap_name':'Entrance-AP'}),'/setup/camera/',fetch_redirect_response=False)
        self.assertRedirects(self.client.post('/setup/camera/',{'name':'Front door','location':'Lobby','camera_source':'1','detection_model':'efficientdet_lite0','confidence_threshold':.45,'process_fps':8}),'/setup/line/',fetch_redirect_response=False)
        self.camera.refresh_from_db();self.assertEqual(self.camera.camera_source,'1');self.assertEqual(self.camera.source_type,'usb')
        self.assertEqual(Device.objects.count(),1)
        self.assertContains(self.client.get('/setup/line/'),'tripwire-canvas')
        self.client.post('/setup/line/',{'line_confirmed':'on'})
        self.profile.refresh_from_db();self.assertEqual(self.profile.setup_stage,3)
        line={'line_ax':.2,'line_ay':.4,'line_bx':.8,'line_by':.4,'entry_side':'positive','line_tolerance':.025,'follow_camera':'on'}
        self.assertRedirects(self.client.post(reverse('tripwire-save',args=[self.camera.pk]),line),'/setup/line/',fetch_redirect_response=False)
        self.assertRedirects(self.client.post('/setup/line/',{'line_confirmed':'on'}),'/dashboard/',fetch_redirect_response=False)
        self.profile.refresh_from_db();self.assertEqual(self.profile.wifi_ap_name,'Entrance-AP');self.assertEqual(self.profile.setup_stage,4)
        self.assertEqual(self.client.get('/dashboard/').status_code,200)
        self.assertRedirects(self.client.get('/'),'/dashboard/',fetch_redirect_response=False)

    def test_ap_name_validates_utf8_bytes_and_controls(self):
        for name in ['','a'*33,'🙂'*9,'AP\x00Name','AP\nName']:
            self.assertFalse(WifiSetupForm({'wifi_ap_name':name}).is_valid())
        self.assertTrue(WifiSetupForm({'wifi_ap_name':'Entrance-AP'}).is_valid())

    def test_single_camera_isolation_and_removed_connections(self):
        self.login()
        self.profile.setup_stage=4;self.profile.save()
        other=Device.objects.create(name='Archived system',source_type='external')
        Detection.objects.create(device=other,direction='in',count=900)
        Detection.objects.create(device=self.camera,direction='in',count=2)
        self.assertEqual(self.client.get('/api/dashboard/').json()['occupancy'],2)
        self.assertEqual(self.client.get('/api/dashboard/').json()['devices_total'],1)
        for path in ['/devices/add/',f'/devices/{other.pk}/',f'/devices/{other.pk}/edit/',f'/devices/{other.pk}/camera/start/',f'/devices/{self.camera.pk}/key/',f'/api/devices/{self.camera.pk}/events/',f'/api/devices/{self.camera.pk}/heartbeat/','/admin/']:
            response=self.client.post(path) if path.endswith(('start/','events/','heartbeat/')) else self.client.get(path)
            self.assertEqual(response.status_code,404,path)
        self.assertNotContains(self.client.get('/dashboard/'),'Archived system')

    def test_bootstrap_never_resets_password_or_setup(self):
        user=get_user_model().objects.get(username='admin');user.set_password('Already-Changed-42!');user.save()
        self.profile.setup_stage=4;self.profile.save()
        call_command('bootstrap_appliance',stdout=io.StringIO())
        user.refresh_from_db();self.assertTrue(user.check_password('Already-Changed-42!'))
        self.profile.refresh_from_db();self.assertEqual(self.profile.setup_stage,4);self.assertEqual(Device.objects.count(),1)

    def test_fresh_bootstrap_selects_actual_usb_index_above_five(self):
        self.profile.active_device = None
        self.profile.save()
        self.camera.delete()
        with patch('vision.cameras.connected_camera_choices', return_value=[('12', 'USB camera 12 · Webcam')]):
            call_command('bootstrap_appliance', stdout=io.StringIO())
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.active_device.camera_source, '12')

    def test_bootstrap_preserves_selected_camera_when_other_usb_camera_is_connected(self):
        with patch('vision.cameras.connected_camera_choices', return_value=[('12', 'USB camera 12 · Webcam')]):
            call_command('bootstrap_appliance', stdout=io.StringIO())
        self.camera.refresh_from_db()
        self.assertEqual(self.camera.camera_source, '0')

    def test_setup_and_password_posts_require_csrf(self):
        user=get_user_model().objects.get(username='admin')
        client=Client(enforce_csrf_checks=True);client.force_login(user)
        self.assertEqual(client.post('/setup/security/',{'new_password1':'New-password-42!'}).status_code,403)
        self.assertEqual(Client(enforce_csrf_checks=True).post('/',{'username':'admin','password':'12345678'}).status_code,403)

    def test_later_password_changes_require_current_password(self):
        self.login();self.security()
        self.assertContains(self.client.get('/setup/security/'),'Old password')
        response=self.client.post('/setup/security/',{'old_password':'wrong','new_password1':'Another-password-42!','new_password2':'Another-password-42!'})
        self.assertEqual(response.status_code,200)

    def test_finished_portal_pages_render_and_logout_revokes_access(self):
        self.login();self.profile.setup_stage=4;self.profile.save()
        for path in ['/dashboard/','/activity/','/analytics/','/alerts/','/settings/',f'/devices/{self.camera.pk}/',f'/devices/{self.camera.pk}/edit/']:
            self.assertEqual(self.client.get(path).status_code,200,path)
        self.client.post('/logout/')
        self.assertRedirects(self.client.get('/dashboard/'),'/',fetch_redirect_response=False)

    def test_selecting_a_camera_again_requires_a_new_line_confirmation(self):
        self.login();self.profile.setup_stage=4;self.profile.line_configured=True;self.profile.save()
        self.client.post('/setup/camera/',{'name':'Front door','location':'Lobby','camera_source':'1','detection_model':'efficientdet_lite0','confidence_threshold':.45,'process_fps':8})
        self.profile.refresh_from_db();self.camera.refresh_from_db()
        self.assertEqual(self.profile.setup_stage,3)
        self.assertFalse(self.profile.line_configured)
        self.assertEqual(self.camera.camera_mode,'tripwire')
        self.assertRedirects(self.client.get('/dashboard/'),'/setup/',fetch_redirect_response=False)

    def test_archived_camera_cannot_claim_a_worker(self):
        from vision.service import claim_camera,CameraError
        archived=Device.objects.create(name='Old webcam',source_type='usb',camera_source='2')
        with self.assertRaises(CameraError):claim_camera(archived.pk)

    def test_skip_setup_opens_dashboard_without_marking_steps_complete(self):
        self.login()
        self.assertContains(self.client.get('/setup/security/'),'Skip for now')
        self.assertEqual(self.client.get('/setup/skip/').status_code,405)
        self.assertRedirects(self.client.post('/setup/skip/'),'/dashboard/',fetch_redirect_response=False)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.setup_stage,0)
        self.assertFalse(self.profile.line_configured)
        self.assertEqual(self.client.get('/dashboard/').status_code,200)
        self.assertContains(self.client.get('/dashboard/'),'My camera')
        self.assertNotContains(self.client.get('/dashboard/'),'connection-card')
        self.assertNotContains(self.client.get('/dashboard/'),'class="workspace"')
        self.assertRedirects(self.client.get('/setup/'),'/setup/security/',fetch_redirect_response=False)
        self.client.post('/logout/');self.login()
        self.assertRedirects(self.client.get('/dashboard/'),'/setup/',fetch_redirect_response=False)

    def test_skip_preserves_saved_progress_and_requires_login_and_csrf(self):
        self.assertRedirects(self.client.post('/setup/skip/'),'/',fetch_redirect_response=False)
        self.login();self.profile.setup_stage=2;self.profile.save()
        self.client.post('/setup/skip/')
        self.profile.refresh_from_db();self.assertEqual(self.profile.setup_stage,2)
        self.assertRedirects(self.client.get('/setup/'),'/setup/camera/',fetch_redirect_response=False)
        protected=Client(enforce_csrf_checks=True)
        protected.force_login(get_user_model().objects.get(username='admin'))
        self.assertEqual(protected.post('/setup/skip/').status_code,403)

    def test_default_hint_is_hidden_after_setup_and_on_fresh_login(self):
        self.assertNotContains(self.client.get('/'),'router-first-login')
        self.client.post('/',{'username':'admin','password':'wrong'})
        self.assertNotContains(self.client.get('/'),'router-first-login')
        self.profile.setup_stage=4;self.profile.save()
        self.assertNotContains(self.client.post('/',{'username':'admin','password':'wrong'}),'router-first-login')
