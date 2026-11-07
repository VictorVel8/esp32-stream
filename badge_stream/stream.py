"""Live scheduling: prefer audio, use fresh video, never replay stalled packets."""
from contextlib import nullcontext
from pathlib import Path
from threading import Event, Thread
import time

from .image import transform
from .legacy import encode, find_port
from .protocol import AVBadge, AUDIO, VIDEO, VOLUME, AUDIO_BYTES


class Stream:
    def __init__(self, capture, buffers, *, port=None, preview=False, fps=15,
                 quality=65, audio=True, seconds=None, video_delay_ms=0, volume=180):
        self.capture, self.buffers = capture, buffers
        self.port, self.preview = port, preview
        self.fps, self.quality, self.audio = fps, quality, audio
        self.seconds = seconds
        self.video_delay_ms = video_delay_ms
        self.volume = volume
        self.settings = (None, "contain", 1.0)
        self.stop = Event()
        self.thread = None
        self.error = None
        self.status = "Ready"
        self.output = None
        self.sent_video = self.sent_audio = 0

    def start(self):
        self.thread = Thread(target=self.run, name="usb-sender", daemon=True)
        self.thread.start()

    def run(self):
        try:
            sink = nullcontext(None) if self.preview else AVBadge(self.port or find_port())
            with sink as badge:
                if badge and self.audio and not badge.audio_ok:
                    raise RuntimeError("Firmware reports speaker initialization failed; try --no-audio to test video")
                if badge and self.audio:
                    badge.send_media(VOLUME, bytes([self.volume]), 0)
                    while badge.pending:
                        badge.poll()
                        self.stop.wait(.002)
                applied_volume = self.volume
                origin = time.monotonic()
                self.buffers.clear()
                deadline = origin + self.seconds if self.seconds else float("inf")
                next_video, report = origin, origin + 1
                while not self.stop.is_set() and time.monotonic() < deadline:
                    self.capture.check()
                    now = time.monotonic()
                    if badge:
                        badge.poll()
                        if self.audio and self.volume != applied_volume and badge.can_send(1):
                            badge.send_media(VOLUME, bytes([self.volume]), 0)
                            applied_volume = self.volume
                    sent = False
                    if self.audio and (not badge or badge.can_send(AUDIO_BYTES)):
                        audio = self.buffers.take_audio(now)
                        if audio and audio[0] >= origin:
                            pts, data = audio
                            if badge:
                                badge.send_media(AUDIO, data, round((pts-origin)*1000))
                            self.sent_audio += 1
                            sent = True
                    video_credit = not badge or badge.can_send(16384)
                    if now >= next_video and video_credit:
                        # Select the current frame only after credit becomes available.
                        frame = self.buffers.take_video()
                        if frame and frame[0] >= origin and now - frame[0] < .25:
                            pts, image = frame
                            crop, fit, zoom = self.settings
                            output = transform(image, crop=crop, fit=fit, zoom=zoom)
                            data = encode(output, self.quality)
                            if badge and not badge.can_send(len(data)):
                                self.buffers.video_drops += 1
                            else:
                                # Encoding can take time: don't send if it became stale.
                                if time.monotonic() - pts < .25:
                                    if badge:
                                        badge.send_media(VIDEO, data, max(0, round((pts-origin)*1000) + self.video_delay_ms))
                                    self.sent_video += 1
                                    self.output = output
                                    if self.preview:
                                        directory = Path(".cache/preview")
                                        directory.mkdir(parents=True, exist_ok=True)
                                        (directory / "latest.jpg").write_bytes(data)
                                    sent = True
                            next_video += 1 / self.fps
                            # Keep cadence without accumulating scheduler drift or
                            # sending a burst to catch up after a long stall.
                            if next_video < now:
                                next_video = now + 1 / self.fps
                    if self.audio and now - origin > 3 and (
                        self.capture.last_audio is None or now - self.capture.last_audio > 3
                    ):
                        raise RuntimeError("No audio samples for 3 seconds; check the selected monitor source")
                    if now >= report:
                        elapsed = now - origin
                        displayed = badge.completed[VIDEO] if badge else self.sent_video
                        telemetry = f" | badge={badge.telemetry}" if badge else ""
                        self.status = (f"{displayed/elapsed:.1f} fps | audio {self.sent_audio} chunks | "
                                       f"dropped video={self.buffers.video_drops}, audio={self.buffers.audio_drops}"
                                       f" | captured={self.capture.video_count}"
                                       f" | input={self.capture.input_count}"
                                       f" | input age={max(0, round((now-self.capture.last_input)*1000)) if self.capture.last_input else -1}ms"
                                       f" | video age={max(0, round((now-self.capture.last_video)*1000)) if self.capture.last_video else -1}ms"
                                       + telemetry)
                        print(self.status, flush=True)
                        report = now + 1
                    if not sent:
                        self.stop.wait(.002)
            self.status = "Stopped"
            displayed = badge.completed[VIDEO] if badge else self.sent_video
            print(f"Stopped cleanly: displayed={displayed}, sent={self.sent_video} video frames, "
                  f"audio={self.sent_audio} chunks", flush=True)
        except Exception as exc:
            self.error = str(exc)
            self.status = f"Stopped: {exc}"
            print(self.status, flush=True)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=7)
