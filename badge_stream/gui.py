"""Local preview and crop selection; USB starts only on Start streaming."""
import time

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk
from PIL import Image, ImageDraw

from .image import transform
from .stream import Stream


def pixbuf(image):
    image = image.convert("RGB")
    return GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(image.tobytes()),
        GdkPixbuf.Colorspace.RGB, False, 8, image.width, image.height, image.width*3)


class Window:
    def __init__(self, capture, buffers, portal, args):
        self.capture, self.buffers, self.portal, self.args = capture, buffers, portal, args
        self.crop = None
        self.drag_start = self.drag_end = None
        self.source_size = None
        self.last_preview = 0
        self.image_rect = (0, 0, 1, 1)
        self.stream = None
        self.audio_pipeline = None
        self.window = Gtk.Window(title="Badge screen + audio — preview" if args.preview else "Badge screen + audio")
        self.window.set_default_size(1050, 740)
        self.window.connect("destroy", self.destroy)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
        self.window.add(box)
        instructions = Gtk.Label(label="Drag around the video in the source preview, then start. Keep Firefox open and the desktop awake.")
        instructions.set_line_wrap(True)
        self.instructions = instructions
        box.pack_start(instructions, False, False, 0)
        row = Gtk.Box(spacing=12)
        box.pack_start(row, True, True, 0)
        self.area = Gtk.EventBox()
        self.source_preview = Gtk.Image()
        self.area.add(self.source_preview)
        self.area.set_size_request(640, 480)
        self.area.set_hexpand(True)
        self.area.set_vexpand(True)
        self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.POINTER_MOTION_MASK)
        self.area.connect("button-press-event", self.press)
        self.area.connect("motion-notify-event", self.motion)
        self.area.connect("button-release-event", self.release)
        row.pack_start(self.area, True, True, 0)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        row.pack_start(side, False, False, 0)
        side.pack_start(Gtk.Label(label="Badge preview (round screen)"), False, False, 0)
        self.badge_preview = Gtk.Image()
        side.pack_start(self.badge_preview, False, False, 0)
        self.fit = Gtk.ComboBoxText()
        for value in ("contain", "cover"):
            self.fit.append_text(value)
        self.fit.set_active(0 if args.fit == "contain" else 1)
        side.pack_start(self.fit, False, False, 0)
        side.pack_start(Gtk.Label(label="Zoom"), False, False, 0)
        self.zoom = Gtk.SpinButton.new_with_range(1, 4, .1)
        self.zoom.set_value(args.zoom)
        side.pack_start(self.zoom, False, False, 0)
        self.reset = Gtk.Button(label="Use whole window")
        self.reset.connect("clicked", self.reset_crop)
        side.pack_start(self.reset, False, False, 0)
        self.audio = Gtk.CheckButton(label="Send browser audio")
        self.audio.set_active(not args.no_audio)
        box.pack_start(self.audio, False, False, 0)
        volume_row = Gtk.Box(spacing=8)
        volume_row.pack_start(Gtk.Label(label="Badge volume:"), False, False, 0)
        self.volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 255, 1)
        self.volume.set_value(args.volume)
        self.volume.set_digits(0)
        volume_row.pack_start(self.volume, True, True, 0)
        box.pack_start(volume_row, False, False, 0)
        audio_row = Gtk.Box(spacing=8)
        audio_row.pack_start(Gtk.Label(label="Audio monitor source:"), False, False, 0)
        self.device = Gtk.Entry()
        self.device.set_text(args.audio_source)
        audio_row.pack_start(self.device, True, True, 0)
        box.pack_start(audio_row, False, False, 0)
        box.pack_start(Gtk.Label(label="@DEFAULT_MONITOR@ includes other PC sounds. Use badge_tiktok.monitor for isolated browser audio."), False, False, 0)
        self.minimize = Gtk.CheckButton(label="Minimize preview when streaming, to keep Firefox visible")
        self.minimize.set_active(True)
        box.pack_start(self.minimize, False, False, 0)
        self.button = Gtk.Button(label="Start preview test" if args.preview else "Start streaming to badge")
        self.button.connect("clicked", self.toggle)
        self.button.set_sensitive(False)
        box.pack_start(self.button, False, False, 0)
        self.status = Gtk.Label(label="Waiting for captured frames…")
        self.status.set_line_wrap(True)
        box.pack_start(self.status, False, False, 0)
        self.started = time.monotonic()
        self.window.show_all()
        print("Preview ready: select the video rectangle and click Start streaming", flush=True)
        GLib.timeout_add(100, self.tick)

    def reset_crop(self, _button):
        self.crop = (0, 0, 1, 1)
        self.button.set_sensitive(True)

    def point(self, event):
        x, y, w, h = self.image_rect
        return max(0, min(1, (event.x-x)/w)), max(0, min(1, (event.y-y)/h))

    def press(self, _widget, event):
        if self.stream and self.stream.thread.is_alive():
            return
        if event.button == 1 and self.capture.preview:
            self.drag_start = self.drag_end = self.point(event)

    def motion(self, _widget, event):
        if self.drag_start:
            self.drag_end = self.point(event)
            self.area.queue_draw()

    def release(self, _widget, event):
        if event.button == 1 and self.drag_start:
            end = self.point(event)
            x0, x1 = sorted((self.drag_start[0], end[0]))
            y0, y1 = sorted((self.drag_start[1], end[1]))
            if x1-x0 > .01 and y1-y0 > .01:
                self.crop = x0, y0, x1, y1
                self.button.set_sensitive(True)
            self.drag_start = self.drag_end = None

    def draw(self):
        if not self.capture.preview:
            return
        image = self.capture.preview[1]
        width, height = self.area.get_allocated_width(), self.area.get_allocated_height()
        scale = min(width/image.width, height/image.height)
        w, h = max(1, round(image.width*scale)), max(1, round(image.height*scale))
        x, y = (width-w)/2, (height-h)/2
        self.image_rect = x, y, w, h
        image = image.resize((w, h), Image.Resampling.BILINEAR)
        crop = self.crop
        if self.drag_start and self.drag_end:
            crop = (*self.drag_start, *self.drag_end)
        if crop:
            x0, y0, x1, y1 = crop
            x0, x1 = sorted((x0, x1))
            y0, y1 = sorted((y0, y1))
            ImageDraw.Draw(image).rectangle((round(x0*w), round(y0*h), round(x1*w), round(y1*h)),
                                            outline=(0, 255, 165), width=2)
        self.source_preview.set_from_pixbuf(pixbuf(image))

    def toggle(self, _button):
        if self.stream and self.stream.thread.is_alive():
            self.stop_stream()
            return
        try:
            self.capture.select_region(self.crop)
            if self.audio.get_active():
                self.audio_pipeline = self.capture.start_audio(self.device.get_text(), synthetic=self.args.synthetic)
            self.stream = Stream(self.capture, self.buffers, port=self.args.port,
                preview=self.args.preview, fps=self.args.fps, quality=self.args.quality,
                audio=self.audio.get_active(), seconds=self.args.seconds,
                video_delay_ms=self.args.video_delay_ms, volume=round(self.volume.get_value()))
            self.stream.settings = None, self.fit.get_active_text(), self.zoom.get_value()
            self.stream.start()
            self.button.set_label("Stop")
            self.audio.set_sensitive(False)
            self.device.set_sensitive(False)
            self.reset.set_sensitive(False)
            self.instructions.set_text("Streaming the selected region. The source selection image is frozen; the badge preview stays live. Stop to select another area.")
            if self.minimize.get_active():
                GLib.timeout_add(300, self.minimize_stream)
        except Exception as exc:
            self.status.set_text(str(exc))
            print(f"Preview: {exc}", flush=True)
            self.stop_audio()

    def minimize_stream(self):
        if self.stream and self.stream.thread.is_alive():
            self.window.iconify()
        return False

    def stop_audio(self):
        if self.audio_pipeline is not None:
            from .capture import Gst
            self.audio_pipeline.set_state(Gst.State.NULL)
            self.capture.pipelines.remove(self.audio_pipeline)
            self.audio_pipeline = None

    def stop_stream(self):
        if self.stream:
            self.stream.close()
        self.capture.select_region(None)
        self.stop_audio()
        self.button.set_label("Start preview test" if self.args.preview else "Start streaming to badge")
        self.audio.set_sensitive(True)
        self.device.set_sensitive(True)
        self.reset.set_sensitive(True)
        self.instructions.set_text("Drag around the video in the source preview, then start. Keep Firefox open and the desktop awake.")

    def tick(self):
        try:
            if self.portal and self.portal.closed:
                raise RuntimeError("Screen sharing ended; select the window again by restarting")
            self.capture.check()
            if self.capture.preview:
                image = self.capture.preview[1]
                source_size = self.capture.source_size or image.size
                if self.source_size and self.source_size != source_size:
                    self.crop = None
                    self.stop_stream()
                    self.button.set_sensitive(False)
                    self.status.set_text("Source size changed; select the video area again")
                self.source_size = source_size
                # Selection preview needs only 5 FPS; during USB streaming 2 FPS
                # avoids competing with Firefox for another full video display.
                active = self.stream and self.stream.thread.is_alive()
                interval = .5 if active else .2
                if time.monotonic() - self.last_preview >= interval or self.drag_start:
                    self.draw()
                    output = (self.stream.output if active and self.stream.output is not None else
                              transform(image, crop=self.crop, fit=self.fit.get_active_text(), zoom=self.zoom.get_value()))
                    mask = Image.new("L", output.size, 0)
                    ImageDraw.Draw(mask).ellipse((0, 0, 239, 239), fill=255)
                    output = Image.composite(output, Image.new("RGB", output.size), mask)
                    self.badge_preview.set_from_pixbuf(pixbuf(output))
                    self.last_preview = time.monotonic()
                if not self.crop:
                    self.status.set_text("Select the video rectangle, or choose Use whole window")
            elif time.monotonic() - self.started > 10:
                raise RuntimeError("No video frames received; check the selected window")
            if self.stream:
                if self.stream.thread.is_alive():
                    self.capture.select_region(self.crop)
                self.stream.settings = None, self.fit.get_active_text(), self.zoom.get_value()
                self.stream.volume = round(self.volume.get_value())
                self.status.set_text(self.stream.status)
                if not self.stream.thread.is_alive():
                    self.stop_stream()
                    if self.stream.error:
                        self.window.present()
            return True
        except Exception as exc:
            self.stop_stream()
            self.status.set_text(str(exc))
            print(f"Preview stopped: {exc}", flush=True)
            self.button.set_sensitive(False)
            return False

    def destroy(self, _widget):
        self.stop_stream()
        Gtk.main_quit()

    def run(self):
        Gtk.main()
