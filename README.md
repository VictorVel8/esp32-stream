# TikTok screen and audio → ESP32 badge

Run Firefox on the mini PC, log into TikTok manually, select its window through GNOME's screen-sharing picker, and draw a rectangle around the video. The PC captures, resizes and encodes video, captures audio, and sends both over USB to the WorkOS init() / M5Stack StopWatch C152 badge.

This is a separate project from `../mini-device-player`. Its JPEGDEC renderer and MPV1 transport were reused from that project's verified 15 FPS implementation. The original project is unchanged.

## Run on the mini PC desktop

Use the mini PC's local terminal with its monitor connected. Python is the system `/usr/bin/python3`, which has the desktop's GI bindings, Pillow and pyserial. A Python environment from the YouTube project does not have these desktop bindings.

```sh
cd /home/vic-mini-pc/projects/auto-scroll-tik-tok

# First: preview only. This never opens the USB device.
/usr/bin/python3 -m badge_stream --preview

# With the audio/video firmware installed:
./run.sh --fps 10

# Video only, same firmware:
/usr/bin/python3 -m badge_stream --no-audio
```

Choose the Firefox window in the desktop picker. Unmaximize it and make it narrow first, to reduce GNOME’s capture workload. In the preview, drag around the video and inspect the circular badge preview. Choose `contain` for bars or `cover` to crop, and adjust zoom as needed. Click **Start streaming to badge**. During streaming the large source selection image freezes; the badge preview stays live. Stop before selecting a different rectangle. Stop using the Stop button or close the preview window. Start/stop does not log you out of Firefox.

The picker offers individual windows. Covering/minimizing the selected window, locking the desktop, unplugging HDMI, or resizing the browser can change capture/playback behavior. Keep the desktop awake and Firefox visible. By default the preview minimizes after starting so it does not cover Firefox; restore it from the taskbar to stop or adjust settings. If the captured source resolution changes, streaming stops and requires a fresh crop.

No TikTok media extraction, account-cookie export, or browser automation is implemented. Auto-scroll is outside this first version. No recording is made during USB streaming. Preview tests replace `.cache/preview/latest.jpg`, rather than accumulating frames; that image may contain whatever was selected for capture.

## Browser audio

By default, `@DEFAULT_MONITOR@` captures the current PC output, including other applications. It does **not** select a microphone. For isolated Firefox audio, create a dedicated virtual output:

```sh
/usr/bin/python3 -m badge_stream.audio create
```

Start a video playing in Firefox, list its audio stream, then move the chosen stream ID:

```sh
/usr/bin/python3 -m badge_stream.audio list
/usr/bin/python3 -m badge_stream.audio move 123  # replace 123 with Firefox's ID
/usr/bin/python3 -m badge_stream --audio-source badge_tiktok.monitor
```

The helper moves only the selected stream; it does not change the system default output. The browser becomes silent on the PC while routed to this virtual output. Firefox may create new audio streams that need moving separately. Remove the virtual output with:

```sh
/usr/bin/python3 -m badge_stream.audio remove
```

Audio is 24 kHz, signed 16-bit little-endian mono PCM, 48,000 bytes/s, in 40 ms chunks. Firmware uses speaker gain 4. The host sets volume 180/255 by default; adjust the live volume slider or use `--volume` from 0–255 (0 mutes). Firmware's idle startup volume is 128. The PC performs conversion; the badge feeds samples through M5Unified to its ES8311 codec and speaker.

Audio and video use capture timestamps and a shared receiver clock, initially targeting 80 ms of scheduling lead, plus device/output buffering. This is experimental synchronization, not measured sample-accurate lip sync. Adjust the picture if necessary:

```sh
# Positive values delay video relative to audio; range -150 to 150 ms.
/usr/bin/python3 -m badge_stream --video-delay-ms 30
```

Large positive video offsets can reduce achieved FPS because the USB window remains bounded. Missing audio chunks are discarded; the speaker returns to silence on underrun. The host reports queue drops, and firmware reports underruns and late-video drops.

## Smoke tests

```sh
# Generated video + quiet 440 Hz PCM tone, no browser, no USB:
/usr/bin/python3 -m badge_stream --synthetic --headless --preview --seconds 5

# Generated audio only on the physical badge:
/usr/bin/python3 -m badge_stream --synthetic --audio-only --seconds 5

# ESP32-generated tone: isolates speaker hardware from PCM capture/transport:
/usr/bin/python3 -m badge_stream --speaker-test --volume 128

# Generated video and audio on the physical badge:
/usr/bin/python3 -m badge_stream --synthetic --headless --seconds 30

# Current PC output → badge speaker, no screen-sharing picker:
/usr/bin/python3 -m badge_stream --audio-only --seconds 30

make test
make build
```

`--port /dev/ttyACM0` overrides automatic Espressif-device discovery. An absent or ambiguous device is reported before streaming. `--fps` defaults to 15, JPEG quality to 65, and output to 240×240. Actual FPS and sync must be benchmarked on hardware with concurrent audio; the original video-only result is not a guarantee for this version.

**Use `./run.sh --fps 10` for the validated live Firefox settings on this mini PC.** A minute-long cropped TikTok run achieved approximately 9.8 displayed FPS, around 32% of one CPU core and 140 MB process RSS. The user confirmed that the long freezes were resolved. The 15 FPS result above was established with generated content; live TikTok at 15 FPS remains unvalidated. Some audio-underrun telemetry occurred during the live test and needs further tuning. Omit `--seconds` for continuous streaming.

## Dependencies and firmware

On Ubuntu, the runtime dependencies are:

```sh
sudo apt-get install python3-gi python3-pil python3-serial gir1.2-gtk-3.0 \
  gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-pipewire gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  pulseaudio-utils
```

On this PC, missing GstVideo typelibs and `pactl` were extracted from Ubuntu packages into ignored `.tools/sysroot`, without modifying system packages. The program and audio helper automatically use those local files. All other runtime dependencies were already installed.

`make build` reuses `../mini-device-player/.tools/arduino-cli` and the installed M5Stack core 3.3.10, M5Unified 0.2.17, M5GFX 0.3.0 and JPEGDEC 1.8.5. Override `BASE` or `CLI` for another checkout. FQBN and partition layout match the working player.

Flashing replaces the application; the sketch never mounts or formats a filesystem. Preserve a verified full-flash backup before flashing. This PC's existing private original backup remains in `../mini-device-player/backups`. A previous video-only application binary is saved locally in ignored `backups/previous-video-player.bin`.

```sh
../mini-device-player/.tools/arduino-cli upload --port /dev/ttyACM0 \
  --fqbn 'm5stack:esp32:m5stack_stopwatch:PSRAM=opi,USBMode=hwcdc,CDCOnBoot=cdc,PartitionScheme=app3M_fat9M_16MB' \
  --input-dir build firmware/usb_player
```

The serial port may change after upload/reset. If access is denied, run this on the mini PC using its current port:

```sh
sudo setfacl -m u:$(id -un):rw /dev/ttyACM0
```

This project uses temporary per-port access; no persistent group membership or udev permissions were installed. On this badge an RTS reset after upload left it in download mode. A watchdog reset booted the application and changed the port:

```sh
../mini-device-player/.venv/bin/python -m esptool --port /dev/ttyACM0 \
  --before no-reset --after watchdog-reset chip-id
```

For subsequent application-only updates with an unchanged partition layout, the validated upload path is:

```sh
../mini-device-player/.venv/bin/python -m esptool --port /dev/ttyACM0 \
  --baud 921600 --after watchdog-reset write-flash 0x10000 build/usb_player.ino.bin
```

To restore the previous video-only application on this development badge (same partition layout):

```sh
../mini-device-player/.venv/bin/python -m esptool --port /dev/ttyACM0 \
  --baud 921600 --after watchdog-reset write-flash 0x10000 backups/previous-video-player.bin
```

For a complete restoration to the original conference firmware and saved state, use that badge's own verified full-flash backup following `../mini-device-player/README.md`.

Button A still freezes the displayed image; audio continues. Button B still cycles brightness. No button mappings were repurposed.

## Transport and bounded latency

The firmware accepts original MPV1 JPEG packets and replies with the original `READY MPV1 466 131072` handshake. The original YouTube sender can therefore be used with this firmware.

MAV1 adds a 24-byte little-endian `<4sIIIII>` header: magic `MAV1`, payload length, sequence, payload CRC32, presentation timestamp in milliseconds, and packet type. Types are 0=hello, 1=JPEG, 2=PCM, 3=stop, 4=volume (one byte), 5=diagnostic tone. Hello, stop and tone have empty payloads. Hello returns `READY MAV1 466 131072 24000 960 audio=<0|1> latency=80`, followed by speaker configuration/power diagnostics.

One PC thread owns USB. At most two packets are outstanding, with their combined size including headers no greater than **131,088 bytes**, preserving the original transport limit. Consequently the largest MAV1 JPEG that can actually be sent is 131,064 bytes. The maximum decoded JPEG payload accepted by firmware remains 128 KiB. Video ACKs follow completed drawing or an explicitly reported stale-frame drop. Audio ACKs mean acceptance into a bounded queue, not completed sound playback. ACKs may arrive out of order because audio and display run independently.

The PC drops excess compositor frames, crops the selected rectangle using GStreamer’s `videocrop`, and scales it to at most 640×640 while preserving aspect ratio **before RGB conversion and copying pixels into Python**. Selection mode runs at 2 FPS with a full-window preview capped at 960×960. While streaming, the full-window selection snapshot freezes; there is no second live full-window preview stream. GNOME still captures the shared window, so a small Firefox window reduces compositor work too. The badge stream defaults to 15 FPS. The PC retains one latest video frame, up to five 40 ms audio chunks, and bounded GStreamer queues. Old video is replaced; stale audio is discarded. Firmware has two video buffers, eight audio queue blocks, and three rotating playback buffers that remain valid while M5Unified uses them. Capture never waits for the badge. On a missing ACK after five seconds, the sender reports sequence and outstanding bytes and closes without automatic packet replay; a timeout alone does not establish a badge crash. Normal stop sends a stop packet after draining outstanding ACKs. A disconnected sender cannot cause infinite audio replay because each PCM block plays once.

See [VALIDATION.md](VALIDATION.md) for actual checks and remaining hardware limitations.

Capture uses two independent GStreamer pipelines. The first receives native PipeWire buffers without crop/size renegotiation. It passes bounded, shared buffer references to the second pipeline, which crops, scales and converts to RGB. Python copies only the reduced RGB output. This separation prevents crop changes from propagating format renegotiation into the desktop capture source—the earlier single-pipeline implementation froze during those changes. Native input and processing queues remain bounded to one frame each, and source buffers are released after processing.
