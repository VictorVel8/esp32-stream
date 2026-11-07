// Renderer derived from VictorVel8/youtube-esp32, snapshot 2026-11-07.
// MPV1 compatibility + MAV1 timestamped JPEG / 24 kHz mono PCM transport.
#include <M5Unified.h>
#include <esp_heap_caps.h>
#include <esp_rom_crc.h>
#include <esp_timer.h>
#include <JPEGDEC.h>
#include <atomic>

constexpr uint32_t MAX_JPEG = 128 * 1024;
constexpr uint32_t RX_CAPACITY = MAX_JPEG + 16;
constexpr size_t AUDIO_SAMPLES = 960, AUDIO_BYTES = AUDIO_SAMPLES * 2;
constexpr uint32_t RATE = 24000, LATENCY_MS = 80;
constexpr uint32_t RX_TIMEOUT = 3000;
struct VideoJob { uint8_t* data; uint32_t size, seq, pts, generation; bool legacy; };
struct AudioJob { int16_t data[AUDIO_SAMPLES]; uint32_t pts, generation; };
VideoJob videoJobs[2];
AudioJob audioJobs[8];
QueueHandle_t freeVideo, readyVideo, freeAudio, readyAudio;
SemaphoreHandle_t txMutex, displayMutex;
std::atomic<uint32_t> generation{0}, underruns{0}, lateVideo{0};
std::atomic<int64_t> epochUs{0};
std::atomic<bool> audioOk{false};
std::atomic<bool> testTone{false};
uint8_t *rxPayload = nullptr, *jpeg = nullptr;
uint32_t payloadSize = 0;
std::atomic<bool> paused{false};
M5Canvas canvas(&M5.Display);
JPEGDEC decoder;
int canvasSize = 0;
uint32_t decodeUs = 0, drawUs = 0;

uint32_t u32(const uint8_t *p) {
  return uint32_t(p[0]) | uint32_t(p[1]) << 8 | uint32_t(p[2]) << 16 | uint32_t(p[3]) << 24;
}
void usbText(const char *text) {
  xSemaphoreTake(txMutex, portMAX_DELAY);
  Serial.print(text);
  xSemaphoreGive(txMutex);
}
void ack(uint32_t seq, const char* kind, uint32_t decode = 0, uint32_t draw = 0) {
  char line[192];
  if (strcmp(kind, "video") == 0) snprintf(line, sizeof(line), "ACK %lu type=%s decode=%lu draw=%lu aq=%u underruns=%lu late=%lu\n",
    (unsigned long)seq, kind, (unsigned long)decode, (unsigned long)draw,
    unsigned(uxQueueMessagesWaiting(readyAudio)), (unsigned long)underruns.load(), (unsigned long)lateVideo.load());
  else snprintf(line, sizeof(line), "ACK %lu type=%s aq=%u underruns=%lu late=%lu\n",
    (unsigned long)seq, kind, unsigned(uxQueueMessagesWaiting(readyAudio)),
    (unsigned long)underruns.load(), (unsigned long)lateVideo.load());
  usbText(line);
}
void error(uint32_t seq) {
  char line[48]; snprintf(line, sizeof(line), "ERR %lu\n", (unsigned long)seq); usbText(line);
}
void status(const char *message) {
  M5.Display.fillScreen(TFT_BLACK);
  M5.Display.setTextDatum(middle_center);
  M5.Display.setTextSize(2);
  M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
  M5.Display.drawString("USB audio + video", 233, 170);
  M5.Display.drawString(message, 233, 230);
  M5.Display.setTextSize(1);
  M5.Display.drawString("A: display freeze   B: brightness", 233, 285);
}
int decodedBlock(JPEGDRAW *block) {
  // JPEGDEC emits big-endian RGB565, matching the sprite's backing storage.
  int width = block->iWidthUsed;
  int height = block->iHeight;
  if (block->x < 0 || block->y < 0 || width < 1 || height < 1 ||
      block->x + width > canvasSize || block->y + height > canvasSize) return 0;
  auto *dst = static_cast<uint16_t *>(canvas.getBuffer());
  for (int y = 0; y < height; ++y) {
    memcpy(dst + (block->y + y) * canvasSize + block->x,
           block->pPixels + y * block->iWidth, width * sizeof(uint16_t));
  }
  return 1;
}

bool render() {
  // The host emits baseline square JPEGs of these bounded dimensions.
  // Determine dimensions before allocating the decoding canvas.
  int size = 0;
  for (size_t i = 2; i + 8 < payloadSize;) {
    if (jpeg[i++] != 0xff) return false;
    uint8_t marker = jpeg[i++];
    if (marker == 0xda || marker == 0xd9) break;
    if (i + 2 > payloadSize) return false;
    size_t len = (size_t(jpeg[i]) << 8) | jpeg[i + 1];
    if (len < 2 || i + len > payloadSize) return false;
    if (marker == 0xc0) {
      if (len < 8) return false;
      int h = (int(jpeg[i + 3]) << 8) | jpeg[i + 4];
      int w = (int(jpeg[i + 5]) << 8) | jpeg[i + 6];
      if (w != h || w < 64 || w > 466) return false;
      size = w;
      break;
    }
    i += len;
  }
  if (!size) return false;
  if (size != canvasSize) {
    canvas.deleteSprite();
    canvasSize = 0;
    if (!canvas.createSprite(size, size)) return false;
    canvas.setPivot(size / 2.0f, size / 2.0f);
    canvasSize = size;
  }
  uint32_t started = micros();
  if (!decoder.openRAM(jpeg, payloadSize, decodedBlock)) return false;
  decoder.setPixelType(RGB565_BIG_ENDIAN);
  bool decoded = decoder.decode(0, 0, 0) != 0;
  decoder.close();
  if (!decoded) return false;
  decodeUs = micros() - started;
  started = micros();
  if (!paused) {
    if (canvasSize == 466) canvas.pushSprite(0, 0);
    else {
      float zoom = 466.0f / canvasSize;
      canvas.pushRotateZoom(233, 233, 0, zoom, zoom);
    }
    M5.Display.waitDMA();
  }
  drawUs = micros() - started;
  return true;
}


// Audio buffers passed to playRaw must remain valid until consumed. Three
// rotating buffers respect M5Unified's documented double-queue ownership.
void audioTask(void*) {
  int16_t playback[3][AUDIO_SAMPLES];
  unsigned index = 0;
  uint32_t currentGeneration = generation.load();
  bool streaming = false;
  while (true) {
    if (currentGeneration != generation.load()) {
      M5.Speaker.stop(0);
      while (M5.Speaker.isPlaying(0)) vTaskDelay(1);
      currentGeneration = generation.load();
      streaming = false;
    }
    if (testTone.exchange(false)) {
      M5.Speaker.tone(880, 1000, 0);
      streaming = false;
    }
    AudioJob* job;
    if (xQueueReceive(readyAudio, &job, pdMS_TO_TICKS(20)) != pdTRUE) {
      if (streaming && !M5.Speaker.isPlaying(0)) {
        ++underruns;
        streaming = false; // M5Unified returns to silence, never repeats a block.
      }
      continue;
    }
    uint32_t gen = job->generation, pts = job->pts;
    memcpy(playback[index], job->data, AUDIO_BYTES);
    xQueueSend(freeAudio, &job, portMAX_DELAY);
    if (gen != generation.load()) continue;
    // Enqueue the next chunk one chunk ahead; first chunk starts at its PTS.
    int64_t due = epochUs.load() + int64_t(pts) * 1000 - (streaming ? 40000 : 0);
    while (esp_timer_get_time() < due && gen == generation.load()) vTaskDelay(1);
    if (gen != generation.load()) continue;
    if (esp_timer_get_time() - due > 200000) continue;
    M5.Speaker.playRaw(playback[index], AUDIO_SAMPLES, RATE, false, 1, 0, false);
    index = (index + 1) % 3;
    streaming = true;
  }
}
void videoTask(void*) {
  while (true) {
    VideoJob* job;
    xQueueReceive(readyVideo, &job, portMAX_DELAY);
    if (job->generation != generation.load()) {
      xQueueSend(freeVideo, &job, portMAX_DELAY);
      continue;
    }
    if (!job->legacy) {
      // Wake slightly before presentation to allow JPEG decode + drawing.
      int64_t due = epochUs.load() + int64_t(job->pts) * 1000 - 45000;
      while (esp_timer_get_time() < due && job->generation == generation.load()) vTaskDelay(1);
      if (job->generation != generation.load()) {
        xQueueSend(freeVideo, &job, portMAX_DELAY);
        continue;
      }
      if (esp_timer_get_time() - due > 150000) {
        ++lateVideo;
        ack(job->seq, "drop");
        xQueueSend(freeVideo, &job, portMAX_DELAY);
        continue;
      }
    }
    jpeg = job->data;
    payloadSize = job->size;
    xSemaphoreTake(displayMutex, portMAX_DELAY);
    bool valid = render();
    xSemaphoreGive(displayMutex);
    if (job->generation == generation.load()) {
      if (valid) ack(job->seq, "video", decodeUs, drawUs);
      else error(job->seq);
    }
    xQueueSend(freeVideo, &job, portMAX_DELAY);
  }
}

void setup() {
  auto cfg = M5.config();
  cfg.serial_baudrate = 0;
  cfg.internal_spk = true;
  cfg.internal_mic = false;
  M5.begin(cfg);
  M5.Display.setBrightness(140);
  canvas.setColorDepth(16);
  canvas.setPsram(true);
  txMutex = xSemaphoreCreateMutex();
  displayMutex = xSemaphoreCreateMutex();
  freeVideo = xQueueCreate(2, sizeof(VideoJob*));
  readyVideo = xQueueCreate(2, sizeof(VideoJob*));
  freeAudio = xQueueCreate(8, sizeof(AudioJob*));
  readyAudio = xQueueCreate(8, sizeof(AudioJob*));
  bool ok = txMutex && displayMutex && freeVideo && readyVideo && freeAudio && readyAudio;
  ok = Serial.setRxBufferSize(RX_CAPACITY) && ok;
  Serial.begin(921600);
  rxPayload = static_cast<uint8_t*>(heap_caps_malloc(MAX_JPEG, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  ok = ok && rxPayload;
  for (auto& job : videoJobs) {
    job.data = static_cast<uint8_t*>(heap_caps_malloc(MAX_JPEG, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    ok = ok && job.data;
    VideoJob* ptr = &job;
    if (freeVideo) xQueueSend(freeVideo, &ptr, 0);
  }
  for (auto& job : audioJobs) {
    AudioJob* ptr = &job;
    if (freeAudio) xQueueSend(freeAudio, &ptr, 0);
  }
  auto spk = M5.Speaker.config();
  spk.task_priority = 5;
  spk.magnification = 4;
  M5.Speaker.config(spk);
  M5.Speaker.setVolume(128);
  audioOk = M5.Speaker.begin() && M5.Speaker.isEnabled();
  status(ok ? "Waiting for computer" : "Allocation failed");
  if (!ok) { rxPayload = nullptr; return; }
  if (audioOk && xTaskCreatePinnedToCore(audioTask, "pcm-feed", 12288, nullptr, 3, nullptr, 0) != pdPASS) audioOk = false;
  if (xTaskCreatePinnedToCore(videoTask, "jpeg-draw", 8192, nullptr, 2, nullptr, 1) != pdPASS) rxPayload = nullptr;
}

void loop() {
  // Serial/buttons share the video task's core; display operations use a mutex.
  M5.update();
  if (M5.BtnA.wasPressed()) paused = !paused;
  if (M5.BtnB.wasPressed()) {
    static uint8_t level = 1;
    const uint8_t brightness[] = {60, 140, 220};
    level = (level + 1) % 3;
    xSemaphoreTake(displayMutex, portMAX_DELAY);
    M5.Display.setBrightness(brightness[level]);
    xSemaphoreGive(displayMutex);
  }
  static uint8_t header[24];
  static size_t headerUsed = 0, used = 0, headerSize = 16;
  static uint32_t size = 0, seq = 0, pts = 0, kind = 0, lastByte = 0;
  static bool modern = false;
  auto reset = [&]() { headerUsed = used = size = 0; };
  if (!rxPayload) { delay(10); return; }
  if (headerUsed && millis() - lastByte > RX_TIMEOUT) {
    usbText("ERR timeout\n"); reset();
  }
  size_t budget = 8192;
  while (Serial.available() && budget) {
    lastByte = millis();
    if (headerUsed >= 4 && headerUsed == headerSize && size) {
      size_t count = std::min(size_t(Serial.available()), size_t(size - used));
      count = std::min(count, budget);
      size_t got = Serial.read(rxPayload + used, count);
      if (!got) break;
      used += got; budget -= got;
      if (used != size) continue;
      if (esp_rom_crc32_le(0, rxPayload, size) != u32(header + 12)) {
        error(seq); reset(); continue;
      }
      if (modern && (kind == 1 || kind == 2) && !epochUs.load()) epochUs = esp_timer_get_time() - int64_t(pts) * 1000 + LATENCY_MS * 1000;
      if (!modern || kind == 1) {
        VideoJob* job;
        if (xQueueReceive(freeVideo, &job, 0) != pdTRUE) error(seq);
        else {
          memcpy(job->data, rxPayload, size);
          job->size = size; job->seq = seq; job->pts = pts;
          job->generation = generation.load(); job->legacy = !modern;
          xQueueSend(readyVideo, &job, portMAX_DELAY);
        }
      } else if (kind == 2 && size == AUDIO_BYTES && audioOk) {
        AudioJob* job;
        if (xQueueReceive(freeAudio, &job, 0) != pdTRUE) error(seq);
        else {
          memcpy(job->data, rxPayload, AUDIO_BYTES);
          job->pts = pts; job->generation = generation.load();
          xQueueSend(readyAudio, &job, portMAX_DELAY);
          ack(seq, "audio"); // Accepted into bounded queue, not yet audible.
        }
      } else if (modern && kind == 4 && size == 1) {
        M5.Speaker.setVolume(rxPayload[0]); ack(seq, "volume");
      } else error(seq);
      reset(); continue;
    }
    uint8_t b = Serial.read(); --budget;
    if (headerUsed < 4) {
      header[headerUsed++] = b;
      if (headerUsed == 4) {
        if (memcmp(header, "MPV1", 4) == 0) { modern = false; headerSize = 16; }
        else if (memcmp(header, "MAV1", 4) == 0) { modern = true; headerSize = 24; }
        else {
          memmove(header, header + 1, 3); headerUsed = 3;
        }
      }
      continue;
    }
    header[headerUsed++] = b;
    if (headerUsed != headerSize) continue;
    size = u32(header + 4); seq = u32(header + 8);
    pts = modern ? u32(header + 16) : 0;
    kind = modern ? u32(header + 20) : 1;
    if (!size) {
      if (!modern) {
        ++generation; epochUs = 0;
        usbText("READY MPV1 466 131072\n");
      } else if (kind == 0) {
        ++generation; epochUs = 0; underruns = 0; lateVideo = 0;
        char line[192];
        snprintf(line, sizeof(line), "READY MAV1 466 131072 24000 960 audio=%u latency=80 board=%u data=%d gain=%u power=%u amp=%u codec=%u\n",
          unsigned(audioOk.load()), unsigned(M5.getBoard()), M5.Speaker.config().pin_data_out,
          unsigned(M5.Speaker.config().magnification), unsigned(M5.In_I2C.readRegister8(0x4F, 0x05, 100000)),
          unsigned(M5.In_I2C.readRegister8(0x4F, 0x06, 100000)), unsigned(M5.In_I2C.readRegister8(0x18, 0x32, 100000)));
        usbText(line);
      } else if (kind == 3) { ++generation; epochUs = 0; ack(seq, "stop"); }
      else if (kind == 5 && audioOk) { testTone = true; ack(seq, "tone"); }
      else error(seq);
      reset();
    } else if (size > MAX_JPEG || (modern && kind != 1 && kind != 2 && kind != 4) ||
               (modern && kind == 2 && size != AUDIO_BYTES) || (modern && kind == 4 && size != 1)) {
      error(seq); reset();
    }
  }
  delay(1);
}
