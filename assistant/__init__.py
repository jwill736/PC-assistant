"""Voice-controlled PC assistant with a HUD dashboard."""

import os as _os

# numpy's OpenBLAS starts one worker thread per core, and they spin after every small matrix product (Smart Turn's
# features): 4x the CPU per check on 4 cores, more on J's 16+. Vesper's matrices are tiny, so one thread is as fast;
# the speech models keep their own onnxruntime threads. Set before anything imports numpy (PERF-3).
_os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
