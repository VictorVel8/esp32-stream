import io
import os
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch
import zlib
import select
import threading
import serial

from PIL import Image

from badge_stream.buffers import MediaBuffers
from badge_stream.image import transform
from badge_stream.legacy import encode
from badge_stream.protocol import (AVBadge, HEADER, AUDIO, VIDEO, HELLO, STOP,
                                  VOLUME, TONE, RX_CAPACITY, AUDIO_BYTES, packet)


class FakeSerial:
    def __init__(self):
        self.rx = bytearray()
        self.written = []
        self.closed = False

    @property
    def in_waiting(self):
        return len(self.rx)

    def read(self, count):
        data = bytes(self.rx[:count])
        del self.rx[:count]
        return data

    def write(self, data):
        self.written.append(data)
        return len(data)

    def close(self):
        self.closed = True


class ProtocolTests(unittest.TestCase):
    def badge(self):
        badge = AVBadge("unused")
        badge.port = FakeSerial()
        return badge

    def test_wire_crc_and_timestamp(self):
        payload = b"\x01\x00" * 960
        wire = packet(AUDIO, payload, 7, 1234)
        self.assertEqual(HEADER.unpack(wire[:24]),
                         (b"MAV1", 1920, 7, zlib.crc32(payload), 1234, AUDIO))
        self.assertEqual(wire[24:], payload)

    def test_invalid_payloads(self):
        for kind, data in [(AUDIO, b"short"), (VIDEO, b""), (HELLO, b"x"),
                           (STOP, b"x"), (99, b"")]:
            with self.assertRaises(ValueError):
                packet(kind, data, 1)

    def test_controls_and_dropped_video_are_not_counted_as_displayed(self):
        badge = self.badge()
        badge.send_media(VOLUME, b"\x80", 0)
        badge.port.rx.extend(b"ACK 1 type=volume\n")
        badge.poll()
        badge.send_media(VIDEO, b"jpeg", 0)
        badge.port.rx.extend(b"ACK 2 type=drop late=1\n")
        badge.poll()
        self.assertEqual(badge.completed[VIDEO], 0)
        self.assertEqual(badge.dropped_video, 1)
        self.assertEqual(packet(TONE, b"", 3)[-4:], struct.pack("<I", TONE))
        for data in (b"", b"\x01\x02"):
            with self.assertRaises(ValueError):
                packet(VOLUME, data, 4)

    def test_window_byte_limit_and_reverse_fragmented_acks(self):
        badge = self.badge()
        self.assertFalse(badge.can_send(RX_CAPACITY-HEADER.size+1))
        badge.send_media(VIDEO, b"jpeg", 0)
        badge.send_media(AUDIO, bytes(AUDIO_BYTES), 0)
        self.assertFalse(badge.can_send(1))
        badge.port.rx.extend(b"ACK 2 aq=3\nACK ")
        badge.poll()
        self.assertEqual(list(badge.pending), [1])
        self.assertEqual(badge.telemetry["aq"], 3)
        badge.port.rx.extend(b"1 decode=12000\n")
        badge.poll()
        self.assertFalse(badge.pending)
        self.assertEqual(badge.completed, {VIDEO: 1, AUDIO: 1})

    def test_missing_ack_does_not_replay(self):
        badge = self.badge()
        badge.send_media(VIDEO, b"jpeg", 0)
        with patch("badge_stream.protocol.time.monotonic", return_value=time.monotonic()+6):
            with self.assertRaisesRegex(RuntimeError, "no automatic replay"):
                badge.poll()
        self.assertEqual(len(badge.port.written), 1)

    def test_unknown_ack_and_device_error_stop(self):
        for line in (b"ACK 999\n", b"ERR timeout\n", b"ACK invalid\n"):
            badge = self.badge()
            badge.port.rx.extend(line)
            with self.assertRaises(RuntimeError):
                badge.poll()

    def test_serial_failure_closes_without_stop_or_replay(self):
        badge = self.badge()
        badge.send_media(VIDEO, b"jpeg", 0)
        badge.__exit__(RuntimeError, RuntimeError("failure"), None)
        self.assertTrue(badge.port.closed)
        self.assertEqual(len(badge.port.written), 1)


class BufferTests(unittest.TestCase):
    def test_capture_burst_is_bounded(self):
        buffers = MediaBuffers()
        for i in range(10000):
            buffers.put_video(i, i)
            buffers.put_audio(i, bytes(1920))
        self.assertEqual(buffers.take_video(), (9999, 9999))
        self.assertEqual(len(buffers.audio), 5)
        self.assertEqual(buffers.video_drops, 9999)
        self.assertEqual(buffers.audio_drops, 9995)

    def test_stale_audio_is_dropped(self):
        buffers = MediaBuffers()
        buffers.put_audio(1, b"old")
        buffers.put_audio(2, b"new")
        self.assertEqual(buffers.take_audio(2.1), (2, b"new"))
        self.assertEqual(buffers.audio_drops, 1)

    def test_crop_fit_baseline_jpeg(self):
        image = Image.new("RGB", (300, 600), "red")
        result = transform(image, crop=(0, .25, 1, .75), fit="contain")
        self.assertEqual(result.size, (240, 240))
        self.assertEqual(result.getpixel((0, 0)), (255, 0, 0))
        result = transform(image, fit="contain")
        self.assertEqual(result.getpixel((0, 120)), (0, 0, 0))
        jpeg = encode(result, 65)
        decoded = Image.open(io.BytesIO(jpeg))
        self.assertFalse(decoded.info.get("progressive", False))
        with self.assertRaises(ValueError):
            transform(image, crop=(.5, 0, .5, 1))


class CaptureIntegrationTests(unittest.TestCase):
    def test_frame_gate_changes_rate_without_timestamp_or_queue_catchup(self):
        from badge_stream.capture import Capture, Gst
        capture = Capture(MediaBuffers(), fps=10, preview_fps=2)
        with patch("badge_stream.capture.time.monotonic", side_effect=[10, 10.1, 10.5, 10.51, 10.52, 10.61, 20, 20.01]):
            self.assertEqual(capture.gate_frame(None, None), Gst.PadProbeReturn.OK)
            self.assertEqual(capture.gate_frame(None, None), Gst.PadProbeReturn.DROP)
            self.assertEqual(capture.gate_frame(None, None), Gst.PadProbeReturn.OK)
            capture.select_region((0, 0, 1, 1))
            self.assertEqual(capture.gate_frame(None, None), Gst.PadProbeReturn.OK)
            self.assertEqual(capture.gate_frame(None, None), Gst.PadProbeReturn.DROP)
            self.assertEqual(capture.gate_frame(None, None), Gst.PadProbeReturn.OK)
            self.assertEqual(capture.gate_frame(None, None), Gst.PadProbeReturn.OK)
            self.assertEqual(capture.gate_frame(None, None), Gst.PadProbeReturn.DROP)

    def test_selected_region_is_cropped_and_scaled_before_rgb(self):
        from badge_stream.capture import Capture
        buffers = MediaBuffers()
        capture = Capture(buffers, preview_fps=2)
        def wait_for(condition):
            deadline = time.monotonic()+4
            while not condition() and time.monotonic() < deadline:
                time.sleep(.02)
                capture.check()
            self.assertTrue(condition())
        try:
            source = "videotestsrc is-live=true pattern=smpte ! video/x-raw,format=BGRx,width=1920,height=1080,framerate=30/1"
            pipeline = capture.start_video(source=source)
            wait_for(lambda: capture.preview is not None)
            source_reconfigurations = []
            from badge_stream.capture import Gst
            def upstream_event(_pad, info):
                if info.get_event().type == Gst.EventType.RECONFIGURE:
                    source_reconfigurations.append(True)
                return Gst.PadProbeReturn.OK
            pipeline.get_by_name("sink").get_static_pad("sink").add_probe(
                Gst.PadProbeType.EVENT_UPSTREAM, upstream_event)
            snapshot = capture.preview
            buffers.clear()
            capture.select_region((.4, .1, .6, .9))
            wait_for(lambda: capture.scale_target == (284, 640) and buffers.video is not None
                     and buffers.video[1].size == (284, 640))
            cropped = capture.processor.get_by_name("region").get_static_pad("src").get_current_caps().get_structure(0)
            scaled = capture.processor.get_by_name("scale_caps").get_static_pad("src").get_current_caps().get_structure(0)
            self.assertEqual((cropped.get_value("width"), cropped.get_value("height")), (384, 864))
            self.assertEqual(scaled.get_value("format"), "BGRx")
            self.assertEqual(capture.source_size, (1920, 1080))
            self.assertIs(capture.preview, snapshot)
            capture.select_region(None)
            wait_for(lambda: capture.preview is not snapshot)
            self.assertEqual(capture.preview[1].size, (960, 540))
            self.assertFalse(source_reconfigurations)
        finally:
            capture.close()

    def test_high_resolution_capture_bounds_buffered_image_and_preserves_aspect(self):
        from badge_stream.capture import Capture
        buffers = MediaBuffers()
        capture = Capture(buffers)
        try:
            source = "videotestsrc is-live=true pattern=black ! video/x-raw,format=BGRx,width=1920,height=1080,framerate=60/1"
            capture.start_video(source=source)
            deadline = time.monotonic()+3
            while capture.video_count < 3 and time.monotonic() < deadline:
                time.sleep(.02)
                capture.check()
            frame = buffers.take_video()
            self.assertIsNotNone(frame)
            self.assertEqual(frame[1].size, (960, 540))
            self.assertLess(capture.video_count, 30)
        finally:
            capture.close()

    def test_real_serial_transport_with_delayed_video_and_immediate_audio_acks(self):
        from badge_stream.capture import Capture
        from badge_stream.stream import Stream
        master, slave = os.openpty()
        stop = threading.Event()
        received = []
        stopped = threading.Event()
        failures = []

        class VirtualSerial(serial.Serial):
            # Pseudo terminals don't implement the badge's DTR/RTS USB controls.
            def _update_dtr_state(self):
                pass
            def _update_rts_state(self):
                pass

        def emulator():
            rx = bytearray()
            delayed = []
            try:
                while not stop.is_set():
                    if select.select([master], [], [], .002)[0]:
                        rx.extend(os.read(master, 65536))
                    while len(rx) >= HEADER.size:
                        magic, length, seq, crc, pts, kind = HEADER.unpack(rx[:HEADER.size])
                        if len(rx) < HEADER.size + length:
                            break
                        data = bytes(rx[HEADER.size:HEADER.size+length])
                        del rx[:HEADER.size+length]
                        if magic != b"MAV1" or crc != zlib.crc32(data):
                            raise AssertionError("Invalid transmitted packet")
                        received.append((seq, kind, pts))
                        if kind == HELLO:
                            os.write(master, b"READY MAV1 466 131072 24000 960 audio=1 latency=80\n")
                        elif kind == VIDEO:
                            delayed.append((time.monotonic()+.04, seq))
                        elif kind == STOP:
                            stopped.set()
                        else:
                            os.write(master, f"ACK {seq} aq=1\n".encode())
                    for due, seq in list(delayed):
                        if time.monotonic() >= due:
                            os.write(master, f"ACK {seq} draw=33000\n".encode())
                            delayed.remove((due, seq))
            except Exception as exc:
                failures.append(exc)

        emulator_thread = threading.Thread(target=emulator)
        emulator_thread.start()
        buffers = MediaBuffers()
        capture = Capture(buffers)
        try:
            capture.start_video(synthetic=True)
            capture.start_audio(synthetic=True)
            with patch("badge_stream.legacy.serial.Serial", VirtualSerial):
                stream = Stream(capture, buffers, port=os.ttyname(slave), seconds=1.5)
                stream.start()
                time.sleep(.3)
                stream.volume = 200
                stream.thread.join(6)
            self.assertFalse(stream.thread.is_alive())
            self.assertIsNone(stream.error)
            self.assertGreater(stream.sent_video, 15)
            self.assertGreater(stream.sent_audio, 25)
            self.assertTrue(stopped.wait(1))
            self.assertFalse(failures)
            self.assertEqual(received[0][1], HELLO)
            self.assertEqual(received[-1][1], STOP)
            self.assertGreaterEqual(sum(kind == VOLUME for _, kind, _ in received), 2)
        finally:
            capture.close()
            stop.set()
            emulator_thread.join(2)
            os.close(master)
            os.close(slave)

    def test_synthetic_video_audio_and_clean_shutdown(self):
        from badge_stream.capture import Capture
        buffers = MediaBuffers()
        capture = Capture(buffers)
        try:
            capture.start_video(synthetic=True)
            capture.start_audio(synthetic=True)
            deadline = time.monotonic()+3
            while (capture.video_count < 5 or capture.audio_count < 5) and time.monotonic() < deadline:
                time.sleep(.02)
                capture.check()
            self.assertGreaterEqual(capture.video_count, 5)
            self.assertGreaterEqual(capture.audio_count, 5)
            self.assertEqual(buffers.take_video()[1].size, (640, 480))
            self.assertEqual(len(buffers.take_audio(time.monotonic())[1]), AUDIO_BYTES)
        finally:
            capture.close()
        self.assertFalse(capture.pipelines)

    def test_preview_stream_writes_only_latest_frame(self):
        from badge_stream.capture import Capture
        from badge_stream.stream import Stream
        buffers = MediaBuffers()
        capture = Capture(buffers)
        try:
            capture.start_video(synthetic=True)
            capture.start_audio(synthetic=True)
            with tempfile.TemporaryDirectory() as directory:
                # Redirect output without changing the process working directory.
                with patch("badge_stream.stream.Path", side_effect=lambda p: Path(directory)/p):
                    stream = Stream(capture, buffers, preview=True, seconds=.6)
                    stream.start()
                    stream.thread.join(4)
                self.assertFalse(stream.thread.is_alive())
                self.assertIsNone(stream.error)
                self.assertGreater(stream.sent_video, 3)
                self.assertGreater(stream.sent_audio, 3)
                self.assertEqual(len(list(Path(directory).rglob("*.jpg"))), 1)
        finally:
            capture.close()


if __name__ == "__main__":
    unittest.main()
