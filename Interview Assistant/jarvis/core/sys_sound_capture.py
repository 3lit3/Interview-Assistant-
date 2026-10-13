"""Loopback capture, isolated in its own PROCESS.

WASAPI virtual endpoints — VB-Cable above all — invalidate their IAudioClient
when the driver glitches. soundcard's next COM call then dies with an access
violation that no try/except can catch; when capture ran inside the app, that
killed the whole process (faulthandler put the stack exactly in soundcard's
GetDevicePeriod, called from its silence-idle path on the 16-channel cable
endpoint).

Running capture here turns that fatal crash into a one-second dropout: the
parent notices this process is gone and spawns a new one. Everything in this
module must stay import-cheap — the spawn start method re-imports it in the
child, and pulling in the app's heavy modules would cost a second of startup
per respawn and drag the whole application graph into the crash-prone process.

The child does NO gating and NO conversion: it ships raw float32 blocks up a
Queue and the parent decides what deserves to reach the model. Dumb on
purpose — the fewer moving parts here, the fewer things a driver glitch can
take down mid-interview.
"""

import time
import warnings

# Duplicated from main.py's SEND_SAMPLE_RATE/CHUNK_SIZE on purpose: importing
# main here would execute the whole application module in the child.
SEND_SAMPLE_RATE = 16000
CHUNK_SIZE = 1024


def capture_loop(chunks, logs, stop) -> None:
    """Record the default speaker's loopback until `stop` is set.

    `chunks` receives float32 little-endian PCM blocks (mono). `logs`
    receives short human-readable status lines. Any capture error reopens
    the device after a second; an access violation kills this process, which
    the parent treats the same way — respawn.
    """
    import ctypes
    import faulthandler
    faulthandler.enable()   # an access violation here dumps the stack, like the app
    try:
        import soundcard as sc
        import numpy as np
        # WASAPI sets DATA_DISCONTINUITY whenever the capture endpoint had a
        # gap. Benign — soundcard still returns usable audio — but under a
        # busy app it fires constantly and would flood the shared console.
        warnings.filterwarnings(
            "ignore", message="data discontinuity in recording",
            category=sc.SoundcardRuntimeWarning)
    except Exception as e:
        logs.put(f"ERR: System sound needs 'pip install soundcard' — {e}")
        while not stop.is_set():
            time.sleep(0.5)
        return

    # soundcard's COM singleton is initialized on whatever thread first
    # imports it; joining MTA here keeps this process's COM lifetime
    # balanced and self-contained.
    try:
        ole32 = ctypes.WinDLL("ole32")
        ole32.CoInitializeEx(None, 0x0)   # COINIT_MULTITHREADED
    except Exception:
        pass

    while not stop.is_set():
        try:
            speaker = sc.default_speaker()
            loopback = sc.get_microphone(id=str(speaker.name),
                                         include_loopback=True)
            logs.put(f"SYS: Listening to system sound — {speaker.name}")
            with loopback.recorder(samplerate=SEND_SAMPLE_RATE,
                                   channels=1) as rec:
                while not stop.is_set():
                    data = rec.record(numframes=CHUNK_SIZE)
                    if data is None:
                        continue
                    mono = np.asarray(data, dtype=np.float32).reshape(-1)
                    if mono.size == 0:
                        continue
                    chunks.put(mono.tobytes())
        except Exception as e:
            logs.put(f"ERR: System sound — {e}. Reopening...")
            time.sleep(1.0)
