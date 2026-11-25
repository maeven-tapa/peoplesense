import json
import math
from types import SimpleNamespace
from unittest import TestCase

from .movement import MovementHistory, MAX_TRACKS, MAX_POINTS, MAX_AGE
from .preview import CAPACITY


def person(identity=1, x=.5, y=.5, hits=3, missed=0):
    return SimpleNamespace(id=identity, point=(x, y), hits=hits, missed=missed)


class MovementHistoryTests(TestCase):
    def test_paths_are_separate_and_only_use_confirmed_observations(self):
        history = MovementHistory()
        self.assertEqual(history.update([person(hits=2)], {1}, 0, 0), [])
        self.assertEqual(history.update([person(x=.3), person(2, x=.7)], {1, 2}, .1, 0), [])
        trails = history.update([person(x=.4), person(2, x=.6)], {1, 2}, .2, 0)
        self.assertEqual(trails, [{'id': 1, 'points': [[.3, .5], [.4, .5]]},
                                  {'id': 2, 'points': [[.7, .5], [.6, .5]]}])
        self.assertEqual(history.update([person(x=.9, missed=1)], {1, 2}, .3, 0), [])
        self.assertEqual(list(history.paths[1])[-1][1], .4)

    def test_tracking_resets_departures_and_long_gaps_do_not_join_people(self):
        history = MovementHistory()
        history.update([person()], {1}, 0, 0)
        self.assertTrue(history.update([person(y=.6)], {1}, .1, 0))
        self.assertEqual(history.update([person(y=.9)], {1}, .2, 1), [])
        self.assertEqual(history.update([], set(), .3, 1), [])
        self.assertFalse(history.paths)
        history.update([person()], {1}, .4, 1)
        self.assertEqual(history.update([person(y=.9)], {1}, 2, 1), [])

    def test_time_point_track_and_ipc_size_bounds(self):
        history = MovementHistory()
        people = [person(i) for i in range(MAX_TRACKS + 20)]
        for index in range(1000):
            trails = history.update(people, set(range(70)), index / 60, 0)
        self.assertEqual(len(history.paths), MAX_TRACKS)
        self.assertTrue(all(len(path) <= MAX_POINTS for path in history.paths.values()))
        self.assertLess(len(json.dumps(trails).encode()), CAPACITY - 1024)
        self.assertTrue(all(1000 / 60 - point[0] <= MAX_AGE for path in history.paths.values() for point in path))
        # At a lower inference rate the age limit, rather than point count, wins.
        history = MovementHistory()
        for index in range(100):
            history.update([person()], {1}, index * .5, 0)
        self.assertLessEqual(len(history.paths[1]), 17)

    def test_invalid_coordinates_never_reach_the_overlay(self):
        history = MovementHistory()
        for x in [math.nan, math.inf, -1, 2]:
            self.assertEqual(history.update([person(x=x)], {1}, 0, 0), [])
        self.assertFalse(history.paths)
