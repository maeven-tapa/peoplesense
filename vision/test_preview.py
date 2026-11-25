import tempfile
import threading
import time
from pathlib import Path
from unittest import TestCase
from .preview import PreviewBuffer, CAPACITY, SIZE


class PreviewBufferTests(TestCase):
    def test_fixed_capacity_and_oversized_frame_does_not_replace_latest(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'preview'
            writer=PreviewBuffer(path,create=True)
            reader=PreviewBuffer(path)
            try:
                self.assertIsNone(reader.read())
                self.assertTrue(writer.publish(b'jpeg',30))
                self.assertFalse(writer.publish(b'x'*(CAPACITY+1),60))
                self.assertEqual(reader.read(),(2,b'jpeg',30))
                self.assertEqual(path.stat().st_size,SIZE)
            finally:reader.close();writer.close()

    def test_reader_never_returns_a_torn_frame(self):
        with tempfile.TemporaryDirectory() as folder:
            writer=PreviewBuffer(Path(folder)/'preview',create=True)
            reader=PreviewBuffer(writer.path)
            done=threading.Event()
            def publish():
                for n in range(1,1001):
                    writer.publish(bytes([n%251])*50000,60)
                    if n%10==0:time.sleep(.0001)
                done.set()
            thread=threading.Thread(target=publish);thread.start()
            reads=0
            try:
                while not done.is_set():
                    frame=reader.read()
                    if frame:
                        self.assertEqual(frame[1],bytes([(frame[0]//2)%251])*50000)
                        reads+=1
                self.assertGreater(reads,0)
            finally:thread.join();reader.close();writer.close()

    def test_slow_inference_does_not_back_up_capture(self):
        import cv2
        import numpy as np
        from .worker import LatestFrames
        class Capture:
            def read(self):
                time.sleep(1/60)
                return True,np.zeros((720,1280,3),dtype=np.uint8)
            def release(self):pass
        with tempfile.TemporaryDirectory() as folder:
            writer=PreviewBuffer(Path(folder)/'preview',create=True)
            reader=PreviewBuffer(writer.path)
            live=LatestFrames(Capture(),writer,60)
            try:
                time.sleep(.6)  # Simulate AI being busy; retain one frame only.
                frame=reader.read()
                self.assertIsNotNone(frame)
                self.assertGreater(frame[0]//2,15)
                self.assertLessEqual(live.frames.qsize(),1)
                decoded=cv2.imdecode(np.frombuffer(frame[1],np.uint8),cv2.IMREAD_COLOR)
                self.assertEqual(decoded.shape,(360,640,3))
            finally:reader.close();live.close()
