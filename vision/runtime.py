"""Avoid allocating large native thread pools for a single camera."""
import os


def configure_native_threads():
    # Set before importing NumPy/MediaPipe; preserve explicit operator settings.
    for name in ['OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS']:
        os.environ.setdefault(name,'1')
