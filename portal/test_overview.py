import uuid
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from portal.models import Device, Detection, PortalSettings, PersonStay, AreaObservation

class OverviewModeTests(TestCase):
    def setUp(self):
        self.now=timezone.now()
        self.camera=Device.objects.create(name='Entrance',location='Area A',source_type='usb',camera_source='0')
        config=PortalSettings.load();config.active_device=self.camera;config.setup_stage=4;config.save()
        self.client.force_login(get_user_model().objects.create_user(username='overview-admin',is_staff=True))

    def test_tripwire_overview_has_three_cards_and_simplified_toolbar(self):
        response=self.client.get(reverse('dashboard'))
        self.assertContains(response,'overview-chart-row')
        self.assertContains(response,'Overview camera')
        self.assertContains(response,'Entrance · Tripwire')
        self.assertContains(response,'People inside')
        self.assertContains(response,'Total entries')
        self.assertContains(response,'Camera preview')
        self.assertNotContains(response,'Traffic analytics</a>')
        self.assertNotContains(response,'>Activity log</a>')
        self.assertNotContains(response,'Illustrated lobby camera preview')

    def test_surveillance_uses_live_people_measured_stays_and_observed_chart(self):
        lease=uuid.uuid4()
        self.camera.camera_mode='surveillance';self.camera.worker_token=lease;self.camera.worker_last_seen=self.now
        self.camera.worker_status='running';self.camera.visible_tracks=4;self.camera.save()
        Detection.objects.create(device=self.camera,direction='in',count=100)
        for i,seconds in enumerate([30,90]):
            PersonStay.objects.create(device=self.camera,worker_token=lease,track_id=str(i),started_at=self.now-timedelta(seconds=seconds),
                last_seen_at=self.now,duration_seconds=seconds,ended=bool(i))
        AreaObservation.objects.create(device=self.camera,count=4,timestamp=self.now-timedelta(minutes=5))
        response=self.client.get(reverse('dashboard'),{'device':self.camera.pk})
        self.assertContains(response,'Current people in area')
        self.assertContains(response,'Average stay in area')
        self.assertContains(response,'1m 0s')
        self.assertNotContains(response,'People inside')
        self.assertNotContains(response,'Total entries')
        self.assertNotContains(response,'Entries &amp; exits')
        self.assertContains(response,'People in area over time')
        data=self.client.get(reverse('dashboard-api'),{'device':self.camera.pk}).json()
        self.assertEqual(data['camera_mode'],'surveillance')
        self.assertEqual(data['selected_device'],self.camera.pk)
        self.assertEqual(data['current_people'],4)
        self.assertEqual(data['average_stay_seconds'],60)
        self.assertEqual(data['stay_count'],2)
        self.assertEqual(data['points'][-1]['occupancy'],4)
        self.assertTrue(data['preview_alive'])

    def test_period_filters_stays_and_chart_without_inventing_live_people(self):
        self.camera.camera_mode='surveillance';self.camera.visible_tracks=7;self.camera.save()
        PersonStay.objects.create(device=self.camera,worker_token=uuid.uuid4(),track_id='old',started_at=self.now-timedelta(days=2),
            last_seen_at=self.now-timedelta(days=2)+timedelta(seconds=90),duration_seconds=90,ended=True)
        AreaObservation.objects.create(device=self.camera,count=7,timestamp=self.now-timedelta(days=2))
        day=self.client.get(reverse('dashboard-api')).json()
        week=self.client.get(reverse('dashboard-api'),{'period':'7d'}).json()
        self.assertEqual(day['current_people'],0)
        self.assertIsNone(day['average_stay_seconds'])
        self.assertEqual(day['average_stay_display'],'—')
        self.assertEqual(week['average_stay_seconds'],90)
        self.assertEqual(max(p['occupancy'] for p in day['points']),0)
        self.assertEqual(max(p['occupancy'] for p in week['points']),7)

    def test_archived_camera_cannot_leak_into_selector_or_metrics(self):
        archived=Device.objects.create(name='Archived area',camera_mode='surveillance')
        AreaObservation.objects.create(device=archived,count=100)
        PersonStay.objects.create(device=archived,worker_token=uuid.uuid4(),track_id='old',started_at=self.now,last_seen_at=self.now,duration_seconds=50)
        self.assertNotContains(self.client.get(reverse('dashboard')),'Archived area')
        data=self.client.get(reverse('dashboard-api'),{'device':archived.pk}).json()
        self.assertIsNone(data['selected_device'])
        self.assertEqual(data['devices_total'],0)
        self.assertIsNone(data['average_stay_seconds'])
