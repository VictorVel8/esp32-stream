# Validation

Initial implementation on 2026-11-07, Linux GNOME/Wayland mini PC.

## Passed before flashing

- `make build`: M5Stack StopWatch C152 firmware compiled using the existing project's toolchain. JPEGDEC renderer retained; separate audio feed and video rendering tasks added.
- New partition binary compared byte-for-byte equal to the previous player's partition binary.
- Existing private original 16 MiB flash backup passed its recorded SHA-256 verification. No original backup was changed.
- `make test`: 13 tests passed, including packet CRC/limits, out-of-order fragmented ACKs, timeout without replay, queue bounds under a 10,000-frame burst, stale audio removal, crop/baseline JPEG output, real GStreamer synthetic capture, bounded preview output, volume controls/drop accounting, and a real pseudo-terminal/pyserial integration run with independent audio and video ACKs.
- Three-second synthetic preview: 45 JPEG frames, 74 PCM chunks, reported 15.0 FPS, zero host video/audio queue drops. This measures PC-side capture/encoding only, not the physical badge.
- Real default-output monitor audio captured successfully through PulseAudio-on-PipeWire without opening USB.
- GTK preview opened, generated video/audio preview ran, and the window and pipelines closed cleanly. Source selection and real TikTok playback still require user interaction.
- No files in `../mini-device-player` were edited. Its existing untracked `TIKTOK_PROPOSAL.md` remains untouched.

## Physical validation

- Initial audio/video firmware uploaded with esptool write verification. Partition table remained equal to the working player. The initial RTS reset left the device in download mode; the previously documented watchdog reset booted it successfully, re-enumerating `/dev/ttyACM0` as `/dev/ttyACM1`.
- Five-second audio-only run: 124 PCM chunks accepted, zero reported underruns, clean stop. The user reported **no audible sound**, so transport success did not validate audio output.
- Initial 30-second synthetic video + PCM run: 419 video frames, 750 PCM chunks, approximately 14 displayed FPS; no missing ACKs, one reported audio underrun, zero reported late-video drops. Startup capture before the handshake contributed 14 discarded audio chunks; statistics now reset at streaming start.
- Dedicated audio sink creation, idempotent creation, monitor capture, and removal tested successfully. No browser stream was moved and no system default sink was changed.
- A second firmware build added adjustable volume (default 128), speaker gain 4, an ESP32-generated diagnostic tone, and speaker power/configuration diagnostics. Application-only upload at `0x10000` completed with written-data verification and watchdog reset. Speaker audibility is pending retest.
- Second build handshake: `audio=1`, board 30 (StopWatch), speaker data pin 21, gain 4, expander power register 157 (codec power enabled), amplifier register 2 (amplifier enabled), codec DAC volume register 191 (expected initialization value). The generated-tone command completed. A subsequent five-second PCM test accepted 125 chunks, with zero host audio drops and zero device underruns. These are software/register checks, not proof of audible output.
- Original YouTube project's existing desktop suite: 29 tests passed unchanged.
- Original MPV1 sender physically tested against the new firmware: 30/30 generated test-pattern frames in 2.00 seconds, 15.0 FPS, mean JPEG decode 12.63 ms and draw 32.92 ms. No modifications to the original sender were needed.
- User confirmed both the ESP32-generated tone and USB-streamed PCM tone were clearly audible, though quiet at volume 128. Host default increased to 180 and a live volume slider added; no additional firmware upload required.
- Final 15-second combined synthetic test: **225 video frames and 375 PCM chunks**, with zero host queue drops, zero device underruns, zero late-video drops, and no missing ACKs. Final telemetry showed roughly 8–11 ms JPEG decode and 34–37 ms draw. Achieved 15 FPS including concurrent audio on this generated content; real TikTok content remains unbenchmarked.
- Desktop ScreenCast picker was launched in preview-only mode; no selection was completed before its two-minute timeout. Real Firefox/TikTok screen capture remains unvalidated.
- User completed a later sharing selection but reported TikTok slowing down before USB streaming started. The capture/preview process used approximately 165% CPU on the Intel N95. Capture was stopped. Pipeline updated to discard excess frames before RGB conversion, downscale to a maximum 960px dimension, and reduce GUI preview to 5 FPS idle / 2 FPS streaming. A generated 4K/60 BGRx input then produced aspect-preserving 960×540 frames at the target rate using approximately 50% of one CPU core, including synthetic source generation; actual desktop retest is pending.

## Early crop and live capture follow-up

- Actual shared source measured **6144×3456**. User reported improved PC playback and live audio after the first CPU reductions, but badge video continued to freeze. Logs showed capture delivering frames in bursts with multi-second gaps, while audio continued and USB ACKs remained responsive.
- Selected rectangles now use `videocrop` before `videoscale` and RGB conversion. Python receives only the scaled portrait region. Full-window selection runs at 2 FPS and retains a frozen selection snapshot during active streaming; no second full-window preview is processed at the stream rate.
- A 6144×3456 generated BGRx source cropped to 614×2764 negotiated to 142×640 **while still BGRx**, before RGB conversion. CPU use was approximately 39% of one core including generation of the full synthetic input. This is a processing benchmark, not a measurement of GNOME's capture overhead.
- Integration regression verifies a 1920×1080 source crops to 384×864, scales to 284×640 before RGB, retains native source coordinates, freezes the selection snapshot, and returns to full selection mode on stop.
- The variable-rate desktop stream continued producing bursty output through `videorate`. Replaced that filter with a monotonic timer pad probe ahead of crop/scale/conversion. It drops excess frames directly and switches 2 FPS selection to the requested streaming rate without video timestamp-based resampling. Live 10 FPS retest is in progress; GNOME/window occlusion behavior remains under investigation.
- Future sharing pickers request individual windows only, preventing accidental monitor sharing. Keep Firefox visible while streaming.

## Working live crop test

- Replacing the frame-rate filter and disabling PipeWire buffer sharing did not resolve the stalls. Logs showed incoming frames stopping immediately after changing crop/size settings. The copying experiment was reverted.
- Split native capture and processing into independent pipelines with an appsink/appsrc handoff. Only shared GStreamer buffer references cross that boundary; native pixel data is not copied into Python. Crop/scale/RGB conversion runs in the second pipeline, so its format changes cannot renegotiate the PipeWire source.
- Repeated five-way crop/selection switching test: native generated source continuously produced 30 FPS; cropped output continuously produced the requested 10 FPS, selection output 2 FPS; **zero upstream source reconfiguration events** for all five changes. Regression coverage also asserts no upstream source reconfiguration when selecting/resetting a crop.
- `make test`: **16 tests passed** after the pipeline isolation change, including real GStreamer capture/crop, source isolation, live volume updates, pseudo-terminal serial multiplexing, packet limits and bounded frame queues.
- **60-second live Firefox/TikTok test at requested 10 FPS:** approximately **9.8 displayed FPS**, approximately **32% of one CPU core**, approximately **140 MB process RSS**. Source frames continued arriving with typical input ages of 0–50 ms and processed-frame ages typically below 120 ms. No missing USB ACKs or late-video drops; five host video frames discarded, zero host audio-queue drops. Firmware reported eight audio-underrun events, which remain a tuning item rather than a claim of uninterrupted audio.
- The user confirmed **“Yes, much better”** when asked whether the badge was consistently moving without the long freezes. The timed test stopped after one minute, then the capture app was closed to release desktop capture resources. No firmware update or new serial permission was needed for these capture changes.
- Working continuous command: `./run.sh --fps 10`. Live 15 FPS and precise lip sync have not been established on TikTok content.

Audio audibility is confirmed by the user. Precise lip sync, long-run stability, browser coverage/minimization behavior, HDMI disconnect behavior, browser-only routing across Firefox stream changes, and sustained 15 FPS with real TikTok content have not yet been established. No persistent serial permissions were installed; temporary per-port ACLs are provided manually by the user.
