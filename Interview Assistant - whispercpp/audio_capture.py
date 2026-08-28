from __future__ import annotations

import asyncio
import queue
import sounddevice as sd
import numpy as np

from config import Config


class AudioCapture:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=64)
        self._thread_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=128)
        self._stream: sd.InputStream | None = None
        self._running = False

    @staticmethod
    def list_devices():
        devices = sd.query_devices()
        print(f"{'idx':>4}  {'name':40}  {'in':>3}  {'out':>3}")
        for i, d in enumerate(devices):
            print(
                f"{i:>4}  {d['name'][:40]:40}  "
                f"{d['max_input_channels']:>3}  {d['max_output_channels']:>3}"
            )
        print("\nFor system/speaker audio, pick a LOOPBACK INPUT device")
        print("(e.g. 'Stereo Mix', 'What U Hear', or a VB-Audio / Virtual Cable device).")

    @staticmethod
    async def test_capture(cfg, duration: float = 5.0):
        sr = cfg.samplerate
        if cfg.device_index is None:
            print("No device selected for test capture.")
            return
        print(f"\n🎤 Recording {duration:.0f}s from device {cfg.device_index} "
              f"({sr} Hz, mono)...")
        try:
            data = sd.rec(int(duration * sr), samplerate=sr, channels=1,
                          dtype="float32", device=cfg.device_index)
            sd.wait()
        except Exception as exc:
            print(f"❌ Capture failed: {exc}")
            return

        pcm = np.clip(data[:, 0] * 32767.0, -32768, 32767).astype("<i2")
        peak = float(np.abs(pcm.astype(np.float32)).max())
        mean_rms = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2) + 1e-9))

        frame = max(1, int(sr * cfg.vad_frame_ms / 1000.0))
        noise_floor = None
        speech_frames = 0
        total_frames = 0
        for i in range(0, len(pcm) - frame, frame):
            f = pcm[i:i + frame].astype(np.float32)
            fr = float(np.sqrt(np.mean(f ** 2) + 1e-9))
            if noise_floor is None:
                noise_floor = fr
            else:
                noise_floor = 0.999 * noise_floor + 0.001 * fr
            thr = max(noise_floor * cfg.vad_noise_multiplier, cfg.vad_min_rms)
            total_frames += 1
            if fr > thr:
                speech_frames += 1

        noise_floor = noise_floor or 0.0
        thr = max(noise_floor * cfg.vad_noise_multiplier, cfg.vad_min_rms)
        verdict = ("✅ Audio IS reaching this device — VAD would trigger."
                   if speech_frames > 0
                   else "⚠️ No/little signal detected — check routing/device selection.")

        print(f"\n--- 5s RMS Test Capture Result ---")
        print(f"Peak amplitude : {peak:.1f} / 32768")
        print(f"Mean RMS       : {mean_rms:.1f}")
        print(f"Noise floor    : {noise_floor:.1f}")
        print(f"VAD threshold  : {thr:.1f}  (noise×{cfg.vad_noise_multiplier} or min {cfg.vad_min_rms})")
        print(f"Speech frames  : {speech_frames}/{total_frames} "
              f"({100 * speech_frames / max(1, total_frames):.0f}%)")
        print(f"{verdict}")
        print(f"Tip: if speech % is 0 but you expected audio, re-pick the device or "
              f"route playback into that input.\n")

    def _callback(self, indata, frames, time_info, status):
        if status:
            pass
        pcm = (indata[:, 0] * 32767.0).clip(-32768, 32767).astype("<i2")
        try:
            self._thread_q.put_nowait(pcm)
        except queue.Full:
            pass

    async def start(self):
        self._running = True
        self.loop = asyncio.get_event_loop()
        kwargs = dict(
            device=self.cfg.device_index,
            samplerate=self.cfg.samplerate,
            channels=self.cfg.channels,
            blocksize=self.cfg.frames_per_buffer,
            dtype="float32",
            callback=self._callback,
        )

        try:
            self._stream = sd.InputStream(**kwargs)
        except sd.PortAudioError as exc:
            self.list_devices()
            raise SystemExit(
                "\nCould not open the selected device as an input. For system/speaker "
                "audio, choose a loopback INPUT device (e.g. 'Stereo Mix', 'What U Hear', "
                "or a VB-Audio / Virtual Cable device), not a Speakers/Output device. "
                "Details: " + str(exc)
            )
        self._stream.start()
        asyncio.create_task(self._drain())

    async def _drain(self):
        while self._running:
            try:
                chunk = await asyncio.get_event_loop().run_in_executor(
                    None, self._thread_q.get, True, 0.05
                )
            except queue.Empty:
                continue
            await self.queue.put(chunk)

    async def get_chunk(self) -> np.ndarray:
        return await self.queue.get()

    async def stop(self):
        self._running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
