import io
import queue
import struct
import threading
import time
from fractions import Fraction

import av
import numpy as np
import pytest

from receiver import BASE, Packet, ReceiverCamera, read_packet


class Fragmented(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 3))


def test_gwhp_chunk_boundaries_extensions_and_clock():
    raw = BASE.pack(b'GWHP', 2, 56, 4, 7, 1920, 1080, 123, 999)
    packet = read_packet(Fragmented(raw + struct.pack('<Q', 456) + b'future00' + b'ABCD'))
    assert packet.payload == b'ABCD'
    assert packet.sequence == 999
    assert packet.pts == 456
    assert packet.sync_valid
    raw = BASE.pack(b'GWHP', 1, 40, 1, 7, 640, 480, 123, 1)
    packet = read_packet(Fragmented(raw+b'X'))
    assert not packet.sync_valid
    assert packet.pts == 123


@pytest.mark.parametrize('magic,version,head,size,width,height', [
    (b'BAD!', 2, 48, 1, 1920, 1080), (b'GWHP', 3, 48, 1, 1920, 1080),
    (b'GWHP', 2, 40, 1, 1920, 1080), (b'GWHP', 2, 257, 1, 1920, 1080),
    (b'GWHP', 2, 48, 17*1024*1024, 1920, 1080), (b'GWHP', 2, 48, 0, 1920, 1080),
    (b'GWHP', 2, 48, 1, 0, 1080), (b'GWHP', 2, 48, 1, 100000, 1080),
])
def test_gwhp_rejects_malformed_headers(magic, version, head, size, width, height):
    with pytest.raises(ValueError):
        read_packet(io.BytesIO(BASE.pack(magic, version, head, size, 0, width, height, 1, 1)))


def test_gwhp_truncation_is_explicit():
    with pytest.raises(EOFError):
        read_packet(io.BytesIO(BASE.pack(b'GWHP', 2, 48, 2, 0, 640, 480, 1, 1)+b'\0'*8+b'X'))


def test_real_cpu_decoder_preserves_packet_timestamp_and_staleness():
    encoder = av.CodecContext.create('libx264', 'w')
    encoder.width, encoder.height = 128, 96
    encoder.pix_fmt = 'yuv420p'
    encoder.time_base = Fraction(1, 1_000_000)
    encoder.options = {'preset': 'ultrafast', 'tune': 'zerolatency'}
    image = np.zeros((96, 128, 3), dtype=np.uint8)
    image[:, :, 1] = 180
    frame = av.VideoFrame.from_ndarray(image, format='rgb24')
    frame.pts, frame.time_base = 123456789, encoder.time_base
    payload = b''.join(bytes(p) for p in encoder.encode(frame))
    camera = ReceiverCamera('http://unused.invalid', 'sender_cam01')
    thread = threading.Thread(target=camera._decode_loop, daemon=True)
    thread.start()
    try:
        packet = Packet(payload, 7, 128, 96, 123456789, 88, 123456999, 2)
        camera.packets.put((1, packet, time.monotonic(), {'sender_id': 'sender', 'camera_id': 'cam01'}))
        deadline = time.monotonic()+3
        while camera.latest is None and time.monotonic() < deadline:
            time.sleep(.01)
        pixels, metadata = camera.frame()
        assert pixels.shape == (96, 128, 3)
        assert metadata['effective_timestamp_us'] == 123456999
        assert metadata['sequence'] == 88
        with camera.lock:
            camera.latest = (camera.latest[0], camera.latest[1], time.monotonic()-2)
        with pytest.raises(ValueError):
            camera.frame()
    finally:
        camera.close()
        thread.join(2)


def test_encoded_buffer_is_bounded():
    camera = ReceiverCamera('http://unused.invalid', 'sender_cam01')
    camera.packets.put_nowait(1)
    camera.packets.put_nowait(2)
    with pytest.raises(queue.Full):
        camera.packets.put_nowait(3)
