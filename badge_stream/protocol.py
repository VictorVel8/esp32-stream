"""Bounded MAV1 multiplexing; the original MPV1 protocol remains supported."""
from collections import OrderedDict
import struct
import time
import zlib

from .legacy import Badge, MAX_JPEG

HEADER = struct.Struct("<4sIIIII")
RX_CAPACITY = MAX_JPEG + 16  # Preserve the original receiver/host byte limit.
HELLO, VIDEO, AUDIO, STOP, VOLUME, TONE = range(6)
RATE = 24000
AUDIO_SAMPLES = 960  # 40 ms of mono S16LE
AUDIO_BYTES = AUDIO_SAMPLES * 2


def packet(kind, data, sequence, pts_ms=0):
    if kind not in (HELLO, VIDEO, AUDIO, STOP, VOLUME, TONE):
        raise ValueError("Unknown MAV1 packet type")
    if kind == VIDEO and not 0 < len(data) <= MAX_JPEG:
        raise ValueError("Invalid JPEG length")
    if kind == AUDIO and len(data) != AUDIO_BYTES:
        raise ValueError("Audio must be 960 mono S16LE samples")
    if kind == VOLUME and len(data) != 1:
        raise ValueError("Volume requires one byte, 0–255")
    if kind in (HELLO, STOP, TONE) and data:
        raise ValueError("Control packets must be empty")
    return HEADER.pack(b"MAV1", len(data), sequence, zlib.crc32(data),
                       pts_ms & 0xffffffff, kind) + data


class AVBadge(Badge):
    """One serial owner, two outstanding packets, ACKs accepted in either order."""
    def __init__(self, port):
        super().__init__(port)
        self.pending = OrderedDict()
        self.sequence = 1
        self.completed = {VIDEO: 0, AUDIO: 0}
        self.telemetry = {}
        self.audio_ok = False
        self.ready = b""
        self.dropped_video = 0

    def __enter__(self):
        try:
            self.port.open()
            self.port.dtr = True
            self.port.reset_input_buffer()
            self.port.write(packet(HELLO, b"", 0))
            line = self.wait_for(b"READY MAV1 466 131072 24000 960", prefix=True, timeout=8)
            self.ready = line
            self.audio_ok = b"audio=1" in line.split()
            self.port.timeout = 0
            return self
        except BaseException:
            self.port.close()
            raise

    @property
    def queued_bytes(self):
        return sum(item[0] for item in self.pending.values())

    def can_send(self, length):
        return len(self.pending) < 2 and self.queued_bytes + HEADER.size + length <= RX_CAPACITY

    def send_media(self, kind, data, pts_ms):
        if not self.can_send(len(data)):
            raise RuntimeError("USB packet/byte window is full")
        sequence = self.sequence
        wire = packet(kind, data, sequence, pts_ms)
        if self.port.write(wire) != len(wire):
            raise RuntimeError("Short USB write; session stopped")
        self.pending[sequence] = (len(wire), time.monotonic(), kind)
        self.sequence = (sequence + 1) & 0xffffffff

    def poll(self):
        self._rx_buffer.extend(self.port.read(self.port.in_waiting or 1))
        if len(self._rx_buffer) > 4096:
            raise RuntimeError("Badge response exceeds 4096 bytes")
        while b"\n" in self._rx_buffer:
            line, _, rest = self._rx_buffer.partition(b"\n")
            self._rx_buffer[:] = rest
            fields = line.split()
            if not fields:
                continue
            if fields[0] == b"ERR":
                raise RuntimeError(f"Badge rejected packet: {line.decode(errors='replace')}")
            if fields[0] != b"ACK" or len(fields) < 2:
                continue
            try:
                seq = int(fields[1])
            except ValueError as exc:
                raise RuntimeError("Malformed badge ACK") from exc
            if seq not in self.pending:
                raise RuntimeError(f"Unexpected ACK {seq}; session stopped")
            _, _, kind = self.pending.pop(seq)
            if kind == VIDEO and b"type=drop" in fields:
                self.dropped_video += 1
            else:
                self.completed[kind] = self.completed.get(kind, 0) + 1
            for field in fields[2:]:
                key, sep, value = field.partition(b"=")
                if sep and value.isdigit():
                    self.telemetry[key.decode()] = int(value)
        now = time.monotonic()
        for seq, (_, sent, _) in self.pending.items():
            if now - sent > 5:
                raise RuntimeError(f"Missing ACK {seq} after 5 s; outstanding={list(self.pending)}, "
                                   f"bytes={self.queued_bytes}. Device state unknown; no automatic replay.")

    def __exit__(self, *exc):
        # STOP is best effort; never append it after a failed/partial USB write.
        # Firmware also expires audio automatically when the sender disappears.
        if exc[0] is None:
            try:
                deadline = time.monotonic() + 1
                while self.pending and time.monotonic() < deadline:
                    self.poll()
                    time.sleep(.002)
                if not self.pending:
                    self.port.write(packet(STOP, b"", self.sequence))
            finally:
                self.port.close()
        else:
            self.port.close()
