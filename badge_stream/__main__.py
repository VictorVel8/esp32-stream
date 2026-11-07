import argparse
import signal
import sys
import time

from .buffers import MediaBuffers


def main():
    p = argparse.ArgumentParser(description="Mirror a selected desktop window and audio to an ESP32 badge")
    p.add_argument("--preview", action="store_true", help="Preview without opening USB; latest JPEG saved to .cache/preview")
    p.add_argument("--port")
    p.add_argument("--fps", type=int, default=15, choices=range(1, 31), metavar="1–30")
    p.add_argument("--quality", type=int, default=65, choices=range(1, 96), metavar="1–95")
    p.add_argument("--fit", choices=["contain", "cover"], default="contain")
    p.add_argument("--zoom", type=float, default=1)
    p.add_argument("--audio-source", default="@DEFAULT_MONITOR@", help="PulseAudio/PipeWire monitor source; never a microphone by default")
    p.add_argument("--no-audio", action="store_true")
    p.add_argument("--volume", type=int, default=180, choices=range(256), metavar="0–255")
    p.add_argument("--speaker-test", action="store_true", help="Play an ESP32-generated tone to test hardware independently of PCM capture")
    p.add_argument("--video-delay-ms", type=int, default=0, choices=range(-150, 151), metavar="-150…150",
                   help="Adjust video relative to audio; positive values delay the picture")
    p.add_argument("--seconds", type=float, help="Stop streaming after this many seconds")
    p.add_argument("--synthetic", action="store_true", help="Use generated video and a quiet 440 Hz tone")
    p.add_argument("--headless", action="store_true", help="No preview UI; requires --synthetic or --audio-only")
    p.add_argument("--audio-only", action="store_true", help="Stream monitor audio without video or portal selection")
    args = p.parse_args()
    if args.speaker_test:
        from .legacy import find_port
        from .protocol import AVBadge, VOLUME, TONE
        try:
            with AVBadge(args.port or find_port()) as badge:
                print(badge.ready.decode(), flush=True)
                if not badge.audio_ok:
                    raise RuntimeError("Speaker initialization failed")
                badge.send_media(VOLUME, bytes([args.volume]), 0)
                while badge.pending:
                    badge.poll()
                    time.sleep(.002)
                badge.send_media(TONE, b"", 0)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    badge.poll()
                    time.sleep(.01)
            print("Speaker diagnostic completed")
            return 0
        except (RuntimeError, OSError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    if not 1 <= args.zoom <= 4:
        p.error("--zoom must be between 1 and 4")
    if args.seconds is not None and not 0 < args.seconds <= 86400:
        p.error("--seconds must be >0 and <=86400")
    if args.headless and not (args.synthetic or args.audio_only):
        p.error("--headless requires --synthetic or --audio-only")
    if args.audio_only and args.no_audio:
        p.error("--audio-only cannot be combined with --no-audio")
    from .capture import Capture, Portal
    from .stream import Stream
    buffers = MediaBuffers()
    capture = Capture(buffers, args.fps, preview_fps=2 if not (args.headless or args.audio_only) else None)
    portal = None
    try:
        if args.headless or args.audio_only:
            if not args.no_audio:
                capture.start_audio(args.audio_source, synthetic=args.synthetic)
            if not args.audio_only:
                capture.start_video(synthetic=True)
            stream = Stream(capture, buffers, port=args.port, preview=args.preview,
                            fps=args.fps, quality=args.quality, audio=not args.no_audio,
                            seconds=args.seconds, video_delay_ms=args.video_delay_ms, volume=args.volume)
            stream.settings = None, args.fit, args.zoom
            signal.signal(signal.SIGINT, lambda *_: stream.stop.set())
            signal.signal(signal.SIGTERM, lambda *_: stream.stop.set())
            stream.start()
            try:
                while stream.thread.is_alive():
                    stream.thread.join(.1)
            finally:
                stream.close()
            if stream.error:
                raise RuntimeError(stream.error)
            print(f"Stopped cleanly: {stream.sent_video} video frames, {stream.sent_audio} audio chunks")
        else:
            from .gui import Window
            if args.synthetic:
                capture.start_video(synthetic=True)
            else:
                portal = Portal()
                fd, node = portal.open()
                capture.start_video(fd, node)
            window = Window(capture, buffers, portal, args)
            from .capture import GLib
            signal.signal(signal.SIGINT, lambda *_: GLib.idle_add(window.window.destroy, priority=GLib.PRIORITY_HIGH))
            signal.signal(signal.SIGTERM, lambda *_: GLib.idle_add(window.window.destroy, priority=GLib.PRIORITY_HIGH))
            window.run()
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped")
    finally:
        capture.close()
        if portal:
            portal.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
