from pathlib import Path
from unittest import SkipTest
import numpy as np
from django.conf import settings
from django.test import SimpleTestCase, override_settings
try:
    import ai_edge_litert
    from .litert_detector import LitePersonDetector, metadata
except ImportError:
    raise SkipTest('Install ai-edge-litert to test the Pi inference backend.')


class LiteDetectorTests(SimpleTestCase):
    def test_pinned_model_decodes_embedded_metadata_and_runs_empty_scene(self):
        for name in ['efficientdet_lite0']:
            from .model_store import model_path
            path=model_path(settings.VISION_MODEL_DIR,name)
            if not path.exists():raise SkipTest('Download pinned models for inference tests.')
            anchors,options,person=metadata(path)
            self.assertEqual(anchors.shape,(options['boxes'],4))
            self.assertEqual(person,0)
            model=LitePersonDetector(settings.VISION_MODEL_DIR,name)
            try:self.assertEqual(model.detect(np.zeros((480,640,3),dtype=np.uint8),0),[])
            finally:model.close()

    @override_settings(VISION_BACKEND='litert')
    def test_factory_uses_litert_and_returns_normalized_person_boxes(self):
        from .detector import PersonDetector
        if not (Path(settings.VISION_MODEL_DIR)/'efficientdet_lite0_int8.tflite').exists():raise SkipTest('Model missing.')
        model=PersonDetector(settings.VISION_MODEL_DIR)
        try:
            self.assertIsInstance(model,LitePersonDetector)
            # A small fake invocation verifies decoding and NMS on real anchor
            # metadata independently of camera/image availability.
            class FakeInterpreter:
                def set_tensor(self,*_):pass
                def invoke(self):pass
                def get_tensor(self,index):
                    if index==model.scores['index']:
                        scores=np.zeros(model.scores['shape'],dtype=np.float32)
                        scores[0,0,model.person_index]=.9;return scores
                    return np.zeros(model.boxes['shape'],dtype=np.float32)
            model.interpreter=FakeInterpreter()
            boxes=model.detect(np.zeros((480,640,3),dtype=np.uint8),0)
            self.assertEqual(len(boxes),1)
            self.assertTrue(all(0<=v<=1 for v in boxes[0]))
            self.assertGreater(boxes[0][2],boxes[0][0]);self.assertGreater(boxes[0][3],boxes[0][1])
        finally:model.close()
