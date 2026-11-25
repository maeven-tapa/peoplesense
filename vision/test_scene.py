from unittest import TestCase, SkipTest
try:
    import cv2
    import numpy as np
    from .scene import SceneAnchor
except ModuleNotFoundError:
    raise SkipTest('Install requirements.txt to run scene alignment tests.')


def doorway():
    random = np.random.default_rng(42)
    image = np.zeros((360,640,3),np.uint8)+55
    for _ in range(350):
        x,y = random.integers([15,15],[625,345])
        cv2.circle(image,(int(x),int(y)),int(random.integers(2,6)),tuple(int(v) for v in random.integers(85,255,3)),-1)
    return image


class SceneAnchorTests(TestCase):
    def test_small_pan_moves_both_endpoints_and_pauses_until_settled(self):
        frame = doorway()
        anchor = SceneAnchor((.2,.5),(.8,.5))
        self.assertFalse(anchor.update(frame,[],0).can_count)
        self.assertTrue(anchor.update(frame,[],1).can_count)
        moved = cv2.warpAffine(frame,np.float32([[1,0,12],[0,1,18]]),(640,360))
        result = anchor.update(moved,[],1.125)
        self.assertEqual(result.status,'moving')
        self.assertFalse(result.can_count)
        np.testing.assert_allclose(result.a,(.2+12/640,.5+18/360),atol=.004)
        np.testing.assert_allclose(result.b,(.8+12/640,.5+18/360),atol=.004)
        result = anchor.update(moved,[],2)
        self.assertEqual(result.status,'locked')
        self.assertTrue(result.can_count)

    def test_small_rotation_and_zoom_follow_reference_without_drift(self):
        frame = doorway()
        anchor = SceneAnchor((.2,.5),(.8,.5))
        anchor.update(frame,[],0)
        matrix = cv2.getRotationMatrix2D((320,180),5,1.03)
        moved = cv2.warpAffine(frame,matrix,(640,360))
        expected = (np.array([[128,180],[512,180]])@matrix[:,:2].T+matrix[:,2])/[640,360]
        for index in range(12):
            result = anchor.update(moved,[],1+index*.125)
            self.assertNotEqual(result.status,'lost')
        np.testing.assert_allclose([result.a,result.b],expected,atol=.004)
        self.assertTrue(result.can_count)

    def test_people_are_excluded_from_background_alignment(self):
        frame = doorway()
        anchor = SceneAnchor((.2,.5),(.8,.5))
        anchor.update(frame,[(.35,.1,.65,.9,.9)],0)
        moved = frame.copy()
        cv2.rectangle(moved,(224,36),(416,324),(255,0,0),-1)
        result = anchor.update(moved,[(.35,.1,.65,.9,.9)],1)
        self.assertTrue(result.can_count)
        np.testing.assert_allclose(result.a,(.2,.5),atol=.002)

    def test_lost_alignment_stays_paused_until_new_reference(self):
        frame = doorway()
        anchor = SceneAnchor((.2,.5),(.8,.5))
        anchor.update(frame,[],0)
        result = anchor.update(np.zeros_like(frame),[],1)
        self.assertEqual(result.status,'lost')
        self.assertFalse(result.can_count)
        self.assertEqual(anchor.update(frame,[],2).status,'lost')
        replacement = SceneAnchor(result.a,result.b)
        replacement.update(frame,[],3)
        self.assertTrue(replacement.update(frame,[],4).can_count)

    def test_blank_or_fully_obscured_scene_cannot_silently_count(self):
        for frame,boxes in [(np.zeros((360,640,3),np.uint8),[]),(doorway(),[(0,0,1,1,.9)])]:
            result = SceneAnchor((.2,.5),(.8,.5)).update(frame,boxes,0)
            self.assertEqual(result.status,'lost')
            self.assertFalse(result.can_count)

    def test_line_outside_view_is_not_clamped_to_wrong_doorway(self):
        frame = doorway()
        anchor = SceneAnchor((.01,.5),(.6,.5))
        anchor.update(frame,[],0)
        moved = cv2.warpAffine(frame,np.float32([[1,0,-20],[0,1,0]]),(640,360))
        result = anchor.update(moved,[],1)
        self.assertEqual(result.status,'lost')
        self.assertFalse(result.can_count)

    def test_changing_aspect_ratio_requires_manual_realignment(self):
        frame = doorway()
        anchor = SceneAnchor((.2,.5),(.8,.5))
        anchor.update(frame,[],0)
        self.assertEqual(anchor.update(frame[:240],[],1).status,'lost')
