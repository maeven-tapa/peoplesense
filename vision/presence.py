"""Anonymous surveillance dwell time, independent of snapshot frequency."""
from django.db import transaction
from django.utils import timezone
from portal.events import EventConflict
from portal.models import Device, PersonStay, AreaObservation

class PresenceRecorder:
    def __init__(self,device,lease,generation):
        self.device,self.lease,self.generation=device,lease,generation
        self.tracks={}
        self.last_flush=self.last_sample=None
        self.last_count=None

    def observe(self,tracks,active_ids,timestamp,now=None):
        now=now or timezone.now()
        for track in tracks:
            state=self.tracks.setdefault(track.id,{'start':now,'first':timestamp,'last':now,'duration':0,'confirmed':False})
            state.update(last=now,duration=max(0,timestamp-state['first']),confirmed=state['confirmed'] or track.hits>=3)
        expired=set(self.tracks)-set(active_ids)
        sample=self.last_sample is None or timestamp-self.last_sample>=5 or len(tracks)!=self.last_count
        flush=sample or bool(expired) or self.last_flush is None or timestamp-self.last_flush>=1
        if not flush:return
        count=len(tracks)
        self.flush(now,expired,count if sample else None)
        self.last_flush=timestamp
        if sample:self.last_sample=timestamp;self.last_count=count
        for track_id in expired:self.tracks.pop(track_id)

    @transaction.atomic
    def flush(self,now,expired=(),sample_count=None):
        if not Device.objects.filter(pk=self.device.pk,worker_token=self.lease,config_version=self.device.config_version,
                camera_mode=Device.Mode.SURVEILLANCE).update(last_seen=now):
            raise EventConflict('Surveillance configuration changed.')
        for track_id,state in self.tracks.items():
            if not state['confirmed']:continue
            PersonStay.objects.update_or_create(device=self.device,track_id=f'{self.lease}:{self.generation}:{track_id}',
                defaults={'worker_token':self.lease,'started_at':state['start'],'last_seen_at':state['last'],
                    'duration_seconds':state['duration'],'ended':track_id in expired})
        if sample_count is not None:
            AreaObservation.objects.create(device=self.device,timestamp=now,count=sample_count)

    def finish(self):
        # Flush the final observed duration only while this config still owns capture.
        try:self.flush(timezone.now(),set(self.tracks))
        except EventConflict:pass
        PersonStay.objects.filter(device=self.device,worker_token=self.lease,track_id__startswith=f'{self.lease}:{self.generation}:').update(ended=True)
