"""MPV1 transport and JPEG encoder reused from ../mini-device-player/player/sender.py.

Original implementation: VictorVel8/youtube-esp32, local snapshot 2026-11-07.
"""
import io
import struct
import time
import zlib
import serial
from serial.tools import list_ports

HEADER = struct.Struct("<4sIII")
MAX_JPEG = 128 * 1024

def packet(data: bytes, sequence: int) -> bytes:
    if len(data) > MAX_JPEG:
        raise ValueError(f"JPEG too large: {len(data)} bytes; reduce --quality or --size")
    return HEADER.pack(b"MPV1", len(data), sequence, zlib.crc32(data)) + data

def find_port():
    ports = [p.device for p in list_ports.comports() if p.vid == 0x303A]
    if len(ports) != 1:
        raise RuntimeError("Connect one badge, or specify --port /dev/ttyACM0. Use --list-ports to inspect.")
    return ports[0]

def encode(image, quality):
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=quality, progressive=False)
    return out.getvalue()

class Badge:
    def __init__(self, port):
        self.port = serial.Serial()
        self.port.port = port
        self.port.baudrate = 921600  # Native USB ignores UART baud rate.
        self.port.timeout = .25
        self.port.write_timeout = 5
        self.port.dtr = False
        self.port.rts = False
        self.last_timings = {}
        self._rx_buffer = bytearray()

    def __enter__(self):
        try:
            self.port.open()
            # Assert DTR for native USB's host-connected check; RTS stays low.
            self.port.dtr = True
            self.port.reset_input_buffer()
            self.port.write(packet(b"", 0))
            self.wait_for(b"READY MPV1 ", prefix=True, timeout=8)
            return self
        except BaseException:
            self.port.close()
            raise

    def __exit__(self, *exc):
        self.port.close()

    def wait_for(self, expected, *, prefix=False, timeout=5):
        deadline = time.monotonic() + timeout
        buffer = self._rx_buffer
        while time.monotonic() < deadline:
            while b"\n" in buffer:
                line, _, rest = buffer.partition(b"\n")
                buffer[:] = rest
                line = line.strip()
                match = line.startswith(expected) if prefix else (
                    line == expected or (expected.startswith(b"ACK ") and line.startswith(expected + b" ")))
                if match:
                    return line
                if line.startswith(b"ERR"):
                    raise RuntimeError(f"Badge rejected frame: {line.decode(errors='replace')}")
            if len(buffer) > 4096:
                buffer.clear()
            buffer.extend(self.port.read(self.port.in_waiting or 1))
        raise RuntimeError(f"No {expected.decode()} response. Is the player firmware running?")

    def send(self, jpeg, sequence):
        self.write_frame(jpeg, sequence)
        self.ack_frame(sequence)

    def write_frame(self, jpeg, sequence):
        self.port.write(packet(jpeg, sequence))

    def ack_frame(self, sequence):
        line = self.wait_for(f"ACK {sequence}".encode())
        self.last_timings = {}
        for field in line.split()[2:]:
            key, sep, value = field.partition(b"=")
            if sep and value.isdigit():
                self.last_timings[key.decode()] = int(value) / 1000
