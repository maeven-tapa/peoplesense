from unittest import TestCase, SkipTest
try:
    from .tracking import PersonTracker, TripwireCounter
except ModuleNotFoundError:
    raise SkipTest('Install requirements.txt to run camera tracking tests.')

def box(x,y):
    return (x-.07,y-.28,x+.07,y,.9)

class TripwireTests(TestCase):
    def simulate(self,positions,**counter_options):
        tracker = PersonTracker()
        counter = TripwireCounter(**counter_options)
        events=[];ids=[]
        for index,point in enumerate(positions):
            detections=[] if point is None else [box(*point)]
            tracks=tracker.update(detections,index*.125)
            ids.extend(track.id for track in tracks)
            events.extend(counter.update(tracks,index*.125,active_ids=set(tracker.tracks)))
        return events,ids
    def test_forward_crossing_counts_once_while_person_remains_visible(self):
        positions=[(.5,y) for y in [.3,.33,.38,.44,.49,.55,.58,.6]]+[(.5,.6)]*40
        events,ids=self.simulate(positions)
        self.assertEqual([event.direction for event in events],['in'])
        self.assertEqual(len(set(ids)),1)
    def test_reverse_crossing_decounts_same_person(self):
        ys=[.3,.33,.38,.44,.49,.55,.58,.6,.6,.6,.6,.56,.5,.44,.41,.37]
        events,ids=self.simulate([(.5,y) for y in ys])
        self.assertEqual([event.direction for event in events],['in','out'])
        self.assertEqual(events[0].track_id,events[1].track_id)
    def test_stationary_person_and_line_jitter_never_count(self):
        events,_=self.simulate([(.5,y) for y in [.4,.4,.4]+[.49,.505,.48,.52,.495]*15])
        self.assertEqual(events,[])
    def test_single_frame_false_transition_is_not_counted(self):
        events,_=self.simulate([(.5,y) for y in [.4,.4,.4,.56,.4,.4]])
        self.assertEqual(events,[])
    def test_initial_appearance_on_either_side_is_not_an_entry(self):
        for side in [.3,.7]:
            events,_=self.simulate([(.5,side)]*20)
            self.assertEqual(events,[])
    def test_outside_segment_does_not_count(self):
        events,_=self.simulate([(.95,y) for y in [.3,.35,.4,.46,.54,.58,.6]])
        self.assertEqual(events,[])
    def test_short_occlusion_keeps_id_and_does_not_recount(self):
        points=[(.5,y) for y in [.3,.33,.38,.44,.49,.55,.58,.6]]+[None]*3+[(.5,.6)]*12
        events,ids=self.simulate(points)
        self.assertEqual([event.direction for event in events],['in'])
        self.assertEqual(len(set(ids)),1)
    def test_expired_track_reappearing_does_not_count_as_new_entry(self):
        points=[(.5,y) for y in [.3,.33,.38,.44,.49,.55,.58,.6]]+[None]*30+[(.5,.6)]*12
        events,ids=self.simulate(points)
        self.assertEqual([event.direction for event in events],['in'])
        self.assertEqual(len(set(ids)),2)
    def test_flipped_entry_direction(self):
        events,_=self.simulate([(.5,y) for y in [.3,.35,.4,.46,.54,.58,.6]],entry_side='negative')
        self.assertEqual([event.direction for event in events],['out'])
    def test_vertical_line(self):
        events,_=self.simulate([(x,.6) for x in [.3,.35,.4,.46,.54,.58,.6]],a=(.5,.1),b=(.5,.9))
        self.assertEqual([event.direction for event in events],['out'])
    def test_two_people_cross_opposite_directions(self):
        tracker=PersonTracker();counter=TripwireCounter();events=[]
        for index,y in enumerate([.3,.33,.38,.44,.49,.55,.58,.6]):
            tracks=tracker.update([box(.3,y),box(.7,.9-y)],index*.125)
            events.extend(counter.update(tracks,index*.125,active_ids=set(tracker.tracks)))
        self.assertEqual(sorted(event.direction for event in events),['in','out'])
        self.assertEqual(len({event.track_id for event in events}),2)
    def test_duplicate_boxes_do_not_spawn_two_tracks(self):
        tracker=PersonTracker();counter=TripwireCounter();events=[]
        for index,y in enumerate([.3,.33,.38,.44,.49,.55,.58,.6]):
            tracks=tracker.update([box(.5,y),box(.505,y)],index*.125)
            events.extend(counter.update(tracks,index*.125,active_ids=set(tracker.tracks)))
        self.assertEqual([event.direction for event in events],['in'])
    def test_fast_return_is_delayed_by_cooldown_but_still_decounts(self):
        events,_=self.simulate([(.5,y) for y in [.4,.4,.4,.54,.56,.44,.43,.42,.41,.4,.4]])
        self.assertEqual([event.direction for event in events],['in','out'])
    def test_lost_tracks_do_not_grow_line_state_forever(self):
        tracker=PersonTracker(max_missed=2);counter=TripwireCounter()
        for index in range(4):
            tracks=tracker.update([box(.5,.4)] if index==0 else [],index*.125)
            counter.update(tracks,index*.125,active_ids=set(tracker.tracks))
        self.assertEqual(counter.states,{})
    def test_invalid_line_is_rejected(self):
        with self.assertRaises(ValueError):
            TripwireCounter(a=(.5,.5),b=(.5,.5))
