"""PNG pre-decode limits shared by local and remote package readers."""
from __future__ import annotations

import struct
import zlib
from .schema import VisualError


def dimensions(header, *, max_pixels=1920 * 1080):
    if (len(header) < 33 or header[:8] != b'\x89PNG\r\n\x1a\n'
            or header[8:12] != b'\x00\x00\x00\r' or header[12:16] != b'IHDR'):
        raise VisualError('invalid_frame', 'Expected PNG IHDR before decoding')
    if zlib.crc32(header[12:29]) & 0xffffffff != struct.unpack('>I', header[29:33])[0]:
        raise VisualError('invalid_frame', 'PNG header checksum differs')
    width, height = struct.unpack('>II', header[16:24])
    if width < 1 or height < 1 or width > 8192 or height > 8192 or width * height > max_pixels:
        raise VisualError('unsupported_input', 'PNG dimensions exceed the pre-decode pixel budget')
    return [width, height]
