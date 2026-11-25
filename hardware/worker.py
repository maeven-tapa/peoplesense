"""One hardware owner: DMA servo PWM plus low-frequency ambient readings."""
import math
import platform
import signal
import time
import uuid
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone
from portal.models import HardwareState
from .service import HardwareError


class PiIO:
    def __init__(self):
        self.factory = self.pan = self.tilt = self.dht = None
        self.sensor_init_error = ''
        try:
            from gpiozero import AngularServo
            from gpiozero.pins.pigpio import PiGPIOFactory
            self.factory = PiGPIOFactory(host='127.0.0.1',port=8888)
            options = dict(initial_angle=None,min_angle=0,max_angle=180,
                min_pulse_width=settings.SERVO_MIN_PULSE,max_pulse_width=settings.SERVO_MAX_PULSE,
                pin_factory=self.factory)
            self.pan = AngularServo(settings.HARDWARE_PAN_PIN,**options)
            self.tilt = AngularServo(settings.HARDWARE_TILT_PIN,**options)
            try:
                import board
                import adafruit_dht
                self.dht = adafruit_dht.DHT11(board.D4,use_pulseio=False)
            except Exception:
                self.sensor_init_error = 'DHT11 unavailable. Check GPIO4 wiring and the Adafruit DHT dependency.'
        except Exception as error:
            self.close()
            raise HardwareError('Cannot initialize servos. Check pigpiod, GPIO permissions, and dependencies.') from error

    def rotate(self, pan, tilt):
        if not all(settings.SERVO_MIN_ANGLE <= angle <= settings.SERVO_MAX_ANGLE for angle in [pan,tilt]):
            raise HardwareError('Servo target is outside the configured limits.')
        self.pan.angle = pan
        self.tilt.angle = tilt

    def read_sensor(self):
        if self.dht is None:raise HardwareError(self.sensor_init_error)
        temperature, humidity = self.dht.temperature, self.dht.humidity
        if temperature is None or humidity is None or not math.isfinite(temperature) or not math.isfinite(humidity):
            raise RuntimeError('Sensor reading incomplete.')
        if not 0 <= temperature <= 50 or not 0 <= humidity <= 100:
            raise RuntimeError('Sensor reading out of range.')
        return float(temperature),float(humidity)

    def close(self):
        for resource in [self.dht,self.pan,self.tilt,self.factory]:
            if resource:
                try:resource.exit() if resource is self.dht else resource.close()
                except Exception:pass


def is_pi4():
    if platform.system() != 'Linux' or platform.machine().lower() not in {'aarch64','armv7l'}:
        return False
    model_path = Path('/proc/device-tree/model')
    return model_path.exists() and model_path.read_text().startswith('Raspberry Pi 4')


def run_hardware(*, max_cycles=None, io_factory=PiIO):
    if not settings.HARDWARE_ENABLED:
        raise HardwareError('Set PEOPLESENSE_HARDWARE=1 on the Raspberry Pi.')
    if not is_pi4():
        raise HardwareError('This hardware profile requires a Raspberry Pi 4.')
    if not (0 <= settings.SERVO_MIN_ANGLE < 90 < settings.SERVO_MAX_ANGLE <= 180
            and .0005 <= settings.SERVO_MIN_PULSE < settings.SERVO_MAX_PULSE <= .0025):
        raise HardwareError('Invalid servo limits or pulse calibration.')
    # The OS lock survives neither crashes nor reboots, unlike a database flag.
    import fcntl
    directory = settings.BASE_DIR/'runtime'/'hardware';directory.mkdir(parents=True,exist_ok=True)
    lock = (directory/'gpio.lock').open('a+')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        lock.close();raise HardwareError('A hardware worker already owns these GPIO pins.')
    lease = uuid.uuid4();io=None;stopped=False
    previous_handlers = {}
    def stop(*_):
        nonlocal stopped
        stopped=True
    state = HardwareState.load()
    HardwareState.objects.filter(pk=1).update(worker_token=lease,worker_ready=False,worker_error='')
    try:
        for sig in [signal.SIGINT,signal.SIGTERM]:
            previous_handlers[sig]=signal.signal(sig,stop)
        io = io_factory()
        # Only the explicit hardware service centers or restores the mount.
        io.rotate(state.pan_target,state.tilt_target)
        HardwareState.objects.filter(pk=1,worker_token=lease).update(worker_ready=True,worker_last_seen=timezone.now(),
            pan_angle=state.pan_target,tilt_angle=state.tilt_target,applied_revision=state.revision,moved_at=timezone.now())
        cycles=0;next_sensor=next_heartbeat=0
        while not stopped:
            close_old_connections()
            state = HardwareState.objects.get(pk=1)
            if state.worker_token != lease:break
            now=time.monotonic()
            if state.revision != state.applied_revision:
                io.rotate(state.pan_target,state.tilt_target)
                HardwareState.objects.filter(pk=1,worker_token=lease).update(pan_angle=state.pan_target,tilt_angle=state.tilt_target,
                    applied_revision=state.revision,moved_at=timezone.now())
            if now >= next_sensor:
                try:
                    temperature,humidity=io.read_sensor()
                    HardwareState.objects.filter(pk=1,worker_token=lease).update(ambient_temperature=temperature,
                        humidity=humidity,sensor_seen_at=timezone.now(),sensor_error='')
                except (RuntimeError,HardwareError):
                    HardwareState.objects.filter(pk=1,worker_token=lease).update(sensor_error='DHT11 reading unavailable. Retrying; check GPIO4 wiring.')
                next_sensor=now+2
            if now >= next_heartbeat:
                HardwareState.objects.filter(pk=1,worker_token=lease).update(worker_last_seen=timezone.now())
                next_heartbeat=now+1
            cycles+=1
            if max_cycles and cycles >= max_cycles:break
            time.sleep(.1)
    except Exception as error:
        message = str(error) if isinstance(error,HardwareError) else 'Hardware worker failed. Check servo power, pigpiod, and wiring.'
        HardwareState.objects.filter(pk=1,worker_token=lease).update(worker_error=message)
        raise HardwareError(message) from error
    finally:
        if io:io.close()
        HardwareState.objects.filter(pk=1,worker_token=lease).update(worker_ready=False,worker_token=None)
        for sig,previous in previous_handlers.items():signal.signal(sig,previous)
        lock.close()
