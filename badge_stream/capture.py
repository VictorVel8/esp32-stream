"""GNOME/Wayland capture through ScreenCast, PipeWire and GStreamer."""
import os
import time
import uuid
from pathlib import Path

import gi
# Optional user-local Ubuntu dependency extraction; no system changes required.
_typelibs = Path(__file__).resolve().parent.parent / ".tools/sysroot/usr/lib/x86_64-linux-gnu/girepository-1.0"
if _typelibs.is_dir():
    gi.Repository.get_default().prepend_search_path(str(_typelibs))
gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gio, GLib, Gst, GstVideo
from PIL import Image

from .protocol import AUDIO_BYTES, RATE

Gst.init(None)
PORTAL = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"


class Portal:
    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.session = None
        self.fd = None
        self.closed = False
        self.subscription = None

    def request(self, method, args, options):
        token = "badge_" + uuid.uuid4().hex
        options = dict(options, handle_token=GLib.Variant("s", token))
        sender = self.bus.get_unique_name()[1:].replace(".", "_")
        request_path = f"{PATH}/request/{sender}/{token}"
        response = []
        sub = self.bus.signal_subscribe(PORTAL, "org.freedesktop.portal.Request", "Response",
                                        request_path, None, Gio.DBusSignalFlags.NONE,
                                        lambda *a: response.append(a[5].unpack()))
        try:
            self.bus.call_sync(PORTAL, PATH, SCREENCAST, method,
                               GLib.Variant(args[0], (*args[1:], options)),
                               None, Gio.DBusCallFlags.NONE, 10000, None)
            deadline = time.monotonic() + 120
            context = GLib.MainContext.default()
            while not response and time.monotonic() < deadline:
                while context.pending():
                    context.iteration(False)
                time.sleep(.01)
            if not response:
                self.bus.call_sync(PORTAL, request_path, "org.freedesktop.portal.Request",
                                   "Close", None, None, Gio.DBusCallFlags.NONE, 2000, None)
                raise RuntimeError("Screen-sharing selection timed out")
            code, results = response[0]
            if code:
                reason = "cancelled" if code == 1 else "failed or denied by the desktop"
                raise RuntimeError(f"Screen sharing {reason} during {method}")
            return results
        finally:
            self.bus.signal_unsubscribe(sub)

    def open(self):
        try:
            result = self.request("CreateSession", ("(a{sv})",), {
                "session_handle_token": GLib.Variant("s", "badge_" + uuid.uuid4().hex)})
            self.session = result["session_handle"]
            self.subscription = self.bus.signal_subscribe(
                PORTAL, "org.freedesktop.portal.Session", "Closed", self.session, None,
                Gio.DBusSignalFlags.NONE, lambda *args: setattr(self, "closed", True))
            self.request("SelectSources", ("(oa{sv})", self.session), {
                "types": GLib.Variant("u", 2),  # Window only: avoid accidental whole-monitor capture.
                "multiple": GLib.Variant("b", False),
                "cursor_mode": GLib.Variant("u", 1)})  # hidden
            result = self.request("Start", ("(osa{sv})", self.session, ""), {})
            node = result["streams"][0][0]
            value, descriptors = self.bus.call_with_unix_fd_list_sync(
                PORTAL, PATH, SCREENCAST, "OpenPipeWireRemote",
                GLib.Variant("(oa{sv})", (self.session, {})), None,
                Gio.DBusCallFlags.NONE, 10000, None, None)
            self.fd = descriptors.get(value.unpack()[0])
            return self.fd, node
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.session and not self.closed:
            try:
                self.bus.call_sync(PORTAL, self.session, "org.freedesktop.portal.Session",
                                   "Close", None, None, Gio.DBusCallFlags.NONE, 2000, None)
            except GLib.Error:
                pass
        if self.subscription is not None:
            self.bus.signal_unsubscribe(self.subscription)
            self.subscription = None
        self.session = None


class Capture:
    def __init__(self, buffers, fps=15, preview_fps=None):
        self.buffers = buffers
        self.fps = fps
        self.pipelines = []
        self.error = None
        self.audio_partial = bytearray()
        self.audio_pts = None
        self.last_video = self.last_audio = None
        self.video_count = self.audio_count = 0
        self.preview = None
        self.source_size = None
        self.scale_target = None
        self.preview_fps = preview_fps or fps
        self.region = None
        self.video_config = None
        self.input_count = 0
        self.last_input = None
        self.next_capture = 0
        self.gate_rate = None
        self.processor = None
        self.raw_input = None
        self.processor_origin = 0
        self.processor_region = None
        self.closing = False

    def gate_frame(self, _pad, _info):
        now = time.monotonic()
        self.input_count += 1
        self.last_input = now
        rate = self.fps if self.region is not None else self.preview_fps
        if rate != self.gate_rate:
            self.gate_rate = rate
            self.next_capture = now
        if now < self.next_capture:
            return Gst.PadProbeReturn.DROP
        self.next_capture += 1/rate
        if self.next_capture < now:
            self.next_capture = now + 1/rate
        return Gst.PadProbeReturn.OK

    def select_region(self, crop):
        if crop is not None:
            x0, y0, x1, y1 = crop
            if not 0 <= x0 < x1 <= 1 or not 0 <= y0 < y1 <= 1:
                raise ValueError("Select a nonempty video rectangle")
        self.region = crop

    @staticmethod
    def timestamp(pipeline, buffer):
        now = time.monotonic()
        if buffer.pts == Gst.CLOCK_TIME_NONE or not pipeline.get_clock():
            return now
        running = pipeline.get_clock().get_time() - pipeline.get_base_time()
        return now - max(0, running - buffer.pts) / Gst.SECOND

    def start_pipeline(self, description, callback, source_device=None):
        pipeline = Gst.parse_launch(description)
        gate = pipeline.get_by_name("frame_gate")
        if gate:
            gate.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self.gate_frame)
        if source_device:
            pipeline.get_by_name("audio_source").set_property("device", source_device)
        sink = pipeline.get_by_name("sink")
        sink.connect("new-sample", callback, pipeline)
        self.pipelines.append(pipeline)
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Capture pipeline could not start")
        return pipeline

    def start_video(self, fd=None, node=None, synthetic=False, source=None):
        if source is None:
            source = ("videotestsrc is-live=true pattern=ball ! video/x-raw,width=640,height=480"
                      if synthetic else f"pipewiresrc fd={fd} path={node} do-timestamp=true "
                                        "min-buffers=4 max-buffers=8")
        return self.start_pipeline(self.video_description(source), self.on_native)

    def video_description(self, source):
        # This pipeline NEVER changes when a crop or output size changes.
        # Only GstBuffer references cross into the separate processing pipeline;
        # native pixels are not copied into Python.
        return (source + ' ! capsfilter caps="video/x-raw,format=(string){BGRx,BGRA,RGBx,RGBA,RGB,BGR,NV12,I420}"'
                " ! appsink name=sink emit-signals=true max-buffers=1 drop=true"
                " sync=false enable-last-sample=false")

    def configure_processor(self, caps, native, region):
        x0, y0, x1, y1 = region or (0, 0, 1, 1)
        left, top = round(x0*native.width), round(y0*native.height)
        right, bottom = native.width-round(x1*native.width), native.height-round(y1*native.height)
        width, height = native.width-left-right, native.height-top-bottom
        if width < 1 or height < 1:
            raise ValueError("Selected video rectangle is too small")
        ratio = min(1, (640 if region is not None else 960)/max(width, height))
        target = max(1, round(width*ratio)), max(1, round(height*ratio))
        config = caps.to_string(), region, target
        if config == self.video_config:
            return
        if self.processor is not None:
            self.processor.set_state(Gst.State.NULL)
            self.pipelines.remove(self.processor)
        self.processor = None
        self.video_config = config
        self.scale_target = target
        self.processor_region = region
        description = (
            "appsrc name=raw is-live=true format=time block=false max-buffers=1"
            " max-bytes=0 max-time=0 leaky-type=downstream"
            f" ! videocrop name=region left={left} top={top} right={right} bottom={bottom}"
            " ! videoscale method=bilinear"
            f" ! capsfilter name=scale_caps caps=video/x-raw,width={target[0]},height={target[1]},pixel-aspect-ratio=1/1"
            " ! videoconvert ! video/x-raw,format=RGB"
            " ! appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false enable-last-sample=false")
        processor = Gst.parse_launch(description)
        raw = processor.get_by_name("raw")
        raw.set_property("caps", caps.copy())
        processor.use_clock(Gst.SystemClock.obtain())
        processor.get_by_name("sink").connect("new-sample", self.on_video, processor)
        self.processor = processor
        self.raw_input = raw
        self.pipelines.append(processor)
        if processor.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Crop processing pipeline could not start")
        self.processor_origin = time.monotonic()
        rate = self.fps if region is not None else self.preview_fps
        print(f"Capture region: {width}×{height} → {target[0]}×{target[1]} at {rate} fps (isolated)", flush=True)

    def on_native(self, sink, pipeline):
        try:
            sample = sink.emit("pull-sample")
            if self.closing or self.gate_frame(None, None) == Gst.PadProbeReturn.DROP:
                return Gst.FlowReturn.OK
            caps = sample.get_caps()
            native = GstVideo.VideoInfo.new_from_caps(caps)
            if native.width < 16 or native.height < 16:
                return Gst.FlowReturn.OK
            self.source_size = native.width, native.height
            self.configure_processor(caps, native, self.region)
            # Shallow copy only the buffer header; GStreamer owns the pixel
            # memory until crop/scale has consumed it. Bounded appsrc drops old
            # buffers without blocking the PipeWire receiving thread.
            forward = sample.get_buffer().copy()
            forward.pts = max(0, round((time.monotonic()-self.processor_origin)*Gst.SECOND))
            forward.dts = Gst.CLOCK_TIME_NONE
            result = self.raw_input.emit("push-buffer", forward)
            if result not in (Gst.FlowReturn.OK, Gst.FlowReturn.FLUSHING):
                raise RuntimeError(f"Native video handoff failed: {result.value_nick}")
            return Gst.FlowReturn.OK
        except Exception as exc:
            self.error = f"Native capture: {exc}"
            return Gst.FlowReturn.ERROR

    def start_audio(self, device="@DEFAULT_MONITOR@", synthetic=False):
        source = ("audiotestsrc is-live=true wave=sine freq=440 volume=0.25 samplesperbuffer=960"
                  if synthetic else "pulsesrc name=audio_source buffer-time=40000 latency-time=10000")
        self.audio_partial.clear()
        self.audio_pts = None
        return self.start_pipeline(source + " ! audioconvert ! audioresample"
                            " ! audio/x-raw,format=S16LE,rate=24000,channels=1,layout=interleaved"
                            " ! queue max-size-buffers=5 max-size-bytes=0 max-size-time=200000000 leaky=downstream"
                            " ! appsink name=sink emit-signals=true max-buffers=5 drop=true sync=false",
                            self.on_audio, None if synthetic else device)

    def on_video(self, sink, pipeline):
        try:
            sample = sink.emit("pull-sample")
            if pipeline != self.processor or self.closing:
                return Gst.FlowReturn.OK
            buffer = sample.get_buffer()
            info = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
            data = buffer.extract_dup(0, buffer.get_size())
            image = Image.frombytes("RGB", (info.width, info.height), data,
                                    "raw", "RGB", info.stride[0], 1)
            pts = self.timestamp(pipeline, buffer)
            if self.processor_region is None:
                self.preview = pts, image
            self.buffers.put_video(pts, image)
            if self.video_count == 0:
                print(f"Video capture active: {info.width}×{info.height}", flush=True)
            self.last_video = time.monotonic()
            self.video_count += 1
            return Gst.FlowReturn.OK
        except Exception as exc:
            self.error = f"Video processing: {exc}"
            return Gst.FlowReturn.ERROR

    def on_audio(self, sink, pipeline):
        try:
            sample = sink.emit("pull-sample")
            buffer = sample.get_buffer()
            pts = self.timestamp(pipeline, buffer)
            # If upstream dropped buffers, don't join samples across the gap.
            expected = (self.audio_pts + len(self.audio_partial) / (RATE * 2)
                        if self.audio_pts is not None else pts)
            if abs(pts - expected) > .02:
                self.audio_partial.clear()
                self.audio_pts = None
            if self.audio_pts is None:
                self.audio_pts = pts
            self.audio_partial.extend(buffer.extract_dup(0, buffer.get_size()))
            while len(self.audio_partial) >= AUDIO_BYTES:
                self.buffers.put_audio(self.audio_pts, bytes(self.audio_partial[:AUDIO_BYTES]))
                del self.audio_partial[:AUDIO_BYTES]
                self.audio_pts += AUDIO_BYTES / (RATE * 2)
            self.last_audio = time.monotonic()
            if self.audio_count == 0:
                print("Audio capture active: 24000 Hz mono PCM", flush=True)
            self.audio_count += 1
            return Gst.FlowReturn.OK
        except Exception as exc:
            self.error = f"Audio capture: {exc}"
            return Gst.FlowReturn.ERROR

    def check(self):
        if self.error:
            raise RuntimeError(self.error)
        for pipeline in tuple(self.pipelines):
            message = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if message:
                if message.type == Gst.MessageType.ERROR:
                    error, _debug = message.parse_error()
                    self.error = f"Capture failed: {error.message}"
                else:
                    self.error = "Capture source disconnected"
                raise RuntimeError(self.error)

    def close(self):
        self.closing = True
        for pipeline in tuple(self.pipelines):
            pipeline.set_state(Gst.State.NULL)
        self.pipelines.clear()
