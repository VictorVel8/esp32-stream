"""Capture buffers never grow with time; old data is replaced or discarded."""
from collections import deque
from threading import Lock


class MediaBuffers:
    def __init__(self, audio_limit=5):
        self.lock = Lock()
        self.video = None
        self.audio = deque(maxlen=audio_limit)
        self.video_drops = self.audio_drops = 0

    def clear(self):
        with self.lock:
            self.video = None
            self.audio.clear()
            self.video_drops = self.audio_drops = 0

    def put_video(self, pts, image):
        with self.lock:
            if self.video is not None:
                self.video_drops += 1
            self.video = pts, image

    def take_video(self):
        with self.lock:
            value, self.video = self.video, None
            return value

    def put_audio(self, pts, data):
        with self.lock:
            if len(self.audio) == self.audio.maxlen:
                self.audio_drops += 1
            self.audio.append((pts, data))

    def take_audio(self, now, max_age=.2):
        with self.lock:
            while self.audio and now - self.audio[0][0] > max_age:
                self.audio.popleft()
                self.audio_drops += 1
            return self.audio.popleft() if self.audio else None
