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
