#!/usr/bin/env python3
"""Tiny PDF writer for Hermes Mail reports.

No external dependencies. Supports plain text, simple vector lines, optional
PNG logo embedding, portrait/landscape pages, and multi-page output.
"""

from __future__ import annotations

import binascii
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reporting_utils import normalize_pdf_text

A4_PORTRAIT = (595.28, 841.89)
A4_LANDSCAPE = (841.89, 595.28)


@dataclass
class PdfImage:
    width: int
    height: int
    rgb_bytes: bytes
    name: str = 'Im0'


@dataclass
class PdfPage:
    width: float
    height: float
    ops: list[str] = field(default_factory=list)

    def text(self, x: float, y: float, text: str, *, size: int = 10, font: str = 'F1') -> None:
        safe = normalize_pdf_text(text)
        self.ops.append(f'BT /{font} {size} Tf {x:.2f} {y:.2f} Td ({safe}) Tj ET')

    def line(self, x1: float, y1: float, x2: float, y2: float, *, width: float = 1.0) -> None:
        self.ops.append(f'{width:.2f} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S')

    def rect(self, x: float, y: float, w: float, h: float, *, stroke: bool = True, fill: bool = False) -> None:
        mode = 'B' if stroke and fill else 'S' if stroke else 'f'
        self.ops.append(f'{x:.2f} {y:.2f} {w:.2f} {h:.2f} re {mode}')

    def image(self, name: str, x: float, y: float, w: float, h: float) -> None:
        self.ops.append(f'q {w:.2f} 0 0 {h:.2f} {x:.2f} {y:.2f} cm /{name} Do Q')

    def raw(self, value: str) -> None:
        self.ops.append(value)


class PdfDocument:
    def __init__(self, *, landscape: bool = False) -> None:
        self.width, self.height = A4_LANDSCAPE if landscape else A4_PORTRAIT
        self.pages: list[PdfPage] = []
        self.fonts = {'F1': 'Helvetica', 'F2': 'Courier', 'F3': 'Helvetica-Bold'}
        self.logo: PdfImage | None = None

    def add_page(self) -> PdfPage:
        page = PdfPage(self.width, self.height)
        self.pages.append(page)
        return page

    def set_logo(self, logo_path: Path) -> bool:
        if not logo_path.exists():
            return False
        try:
            self.logo = load_png_as_rgb(logo_path)
            return True
        except Exception:
            self.logo = None
            return False

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        objects: list[bytes] = []

        # 1: catalog, 2: pages tree, 3..: fonts, optional image, then page/content pairs.
        font_object_numbers: dict[str, int] = {}
        next_obj = 1
        catalog_obj = next_obj
        next_obj += 1
        pages_obj = next_obj
        next_obj += 1
        for font_name in self.fonts:
            font_object_numbers[font_name] = next_obj
            next_obj += 1
        image_obj = None
        if self.logo is not None:
            image_obj = next_obj
            next_obj += 1

        page_pairs: list[tuple[int, int]] = []
        for _ in self.pages:
            content_obj = next_obj
            page_obj = next_obj + 1
            next_obj += 2
            page_pairs.append((page_obj, content_obj))

        # Object builder helpers.
        def obj(n: int, body: bytes | str) -> bytes:
            if isinstance(body, str):
                body_b = body.encode('latin-1')
            else:
                body_b = body
            return f'{n} 0 obj\n'.encode('latin-1') + body_b + b'\nendobj\n'

        # Catalog/pages/fonts/image.
        objects.append(obj(catalog_obj, f'<< /Type /Catalog /Pages {pages_obj} 0 R >>'))

        kids = ' '.join(f'{page_obj} 0 R' for page_obj, _ in page_pairs)
        objects.append(obj(pages_obj, f'<< /Type /Pages /Kids [{kids}] /Count {len(page_pairs)} >>'))

        for font_name, base_font in self.fonts.items():
            font_num = font_object_numbers[font_name]
            objects.append(obj(font_num, f'<< /Type /Font /Subtype /Type1 /BaseFont /{base_font} >>'))

        if self.logo is not None and image_obj is not None:
            stream = zlib.compress(self.logo.rgb_bytes)
            image_dict = (
                f'<< /Type /XObject /Subtype /Image /Width {self.logo.width} /Height {self.logo.height} '
                f'/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length {len(stream)} >>\n'
            ).encode('latin-1')
            objects.append(f'{image_obj} 0 obj\n'.encode('latin-1') + image_dict + b'stream\n' + stream + b'\nendstream\nendobj\n')

        for (page_obj, content_obj), page in zip(page_pairs, self.pages):
            content_bytes = '\n'.join(page.ops).encode('latin-1')
            content_stream = b'<< /Length ' + str(len(content_bytes)).encode('latin-1') + b' >>\nstream\n' + content_bytes + b'\nendstream'
            objects.append(obj(content_obj, content_stream))
            resource_fonts = ' '.join(f'/{name} {font_object_numbers[name]} 0 R' for name in self.fonts)
            resource_parts = [f'/Font << {resource_fonts} >>']
            if self.logo is not None and image_obj is not None:
                resource_parts.append(f'/XObject << /{self.logo.name} {image_obj} 0 R >>')
            resources = '<< ' + ' '.join(resource_parts) + ' >>'
            page_body = (
                f'<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 {page.width:.2f} {page.height:.2f}] '
                f'/Resources {resources} /Contents {content_obj} 0 R >>'
            )
            objects.append(obj(page_obj, page_body))

        # Build file with xref.
        header = b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n'
        offsets = [0]
        body = bytearray(header)
        for entry in objects:
            offsets.append(len(body))
            body.extend(entry)
        xref_start = len(body)
        total = len(objects) + 1
        xref = [b'xref\n', f'0 {total}\n'.encode('latin-1'), b'0000000000 65535 f \n']
        for offset in offsets[1:]:
            xref.append(f'{offset:010d} 00000 n \n'.encode('latin-1'))
        trailer = (
            f'trailer\n<< /Size {total} /Root {catalog_obj} 0 R >>\nstartxref\n{xref_start}\n%%EOF\n'
        ).encode('latin-1')
        body.extend(b''.join(xref))
        body.extend(trailer)
        path.write_bytes(bytes(body))
        return path


def load_png_as_rgb(path: Path) -> PdfImage:
    raw = path.read_bytes()
    if raw[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('not a PNG')
    width = height = None
    bit_depth = color_type = None
    idat = bytearray()
    pos = 8
    while pos < len(raw):
        length = struct.unpack('>I', raw[pos:pos + 4])[0]
        pos += 4
        chunk_type = raw[pos:pos + 4]
        pos += 4
        data = raw[pos:pos + length]
        pos += length
        _crc = raw[pos:pos + 4]
        pos += 4
        if chunk_type == b'IHDR':
            width, height, bit_depth, color_type, _compression, _filter, _interlace = struct.unpack('>IIBBBBB', data)
        elif chunk_type == b'IDAT':
            idat.extend(data)
        elif chunk_type == b'IEND':
            break
    if width is None or height is None:
        raise ValueError('invalid PNG')
    if bit_depth != 8:
        raise ValueError('unsupported PNG bit depth')
    decompressed = zlib.decompress(bytes(idat))
    if color_type == 2:
        rgb = _defilter_png_rgb(decompressed, width, height, 3)
    elif color_type == 6:
        rgba = _defilter_png_rgb(decompressed, width, height, 4)
        rgb = _strip_alpha(rgba)
    else:
        raise ValueError(f'unsupported PNG color type: {color_type}')
    return PdfImage(width=width, height=height, rgb_bytes=rgb)


def _strip_alpha(rgba: bytes) -> bytes:
    rgb = bytearray()
    for i in range(0, len(rgba), 4):
        rgb.extend(rgba[i:i + 3])
    return bytes(rgb)


def _defilter_png_rgb(data: bytes, width: int, height: int, channels: int) -> bytes:
    stride = width * channels
    out = bytearray()
    prev = bytearray(stride)
    pos = 0
    for _y in range(height):
        filter_type = data[pos]
        pos += 1
        row = bytearray(data[pos:pos + stride])
        pos += stride
        if filter_type == 0:
            pass
        elif filter_type == 1:
            for i in range(stride):
                left = row[i - channels] if i >= channels else 0
                row[i] = (row[i] + left) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = row[i - channels] if i >= channels else 0
                up = prev[i]
                row[i] = (row[i] + ((left + up) // 2)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                a = row[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                row[i] = (row[i] + _paeth(a, b, c)) & 0xFF
        else:
            raise ValueError(f'unsupported PNG filter: {filter_type}')
        out.extend(row)
        prev = row
    return bytes(out)


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c
