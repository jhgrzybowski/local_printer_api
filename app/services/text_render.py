"""Lay out plain text as a PDF with an embedded Unicode font.

CUPS prints text with non-embedded Courier in a Latin-1 encoding, which turns
Polish and other non-Latin-1 letters into garbage, and it paginates the text
only while printing, so nothing can be previewed. Rendering the text once at
upload gives the same PDF for preview and print, with every letter the font
covers. The whole TrueType font is embedded, so no extra libraries are needed.
"""
from __future__ import annotations

import bisect
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeMono.ttf",
)
# A4 with 2 cm margins and 10 pt type: 80 columns and 60 lines, like a
# typewriter page.
PAGE_WIDTH, PAGE_HEIGHT = 595.28, 841.89
MARGIN = 56.69
FONT_SIZE = 10.0
LEADING = 12.0
TAB_SIZE = 8
MAX_PAGES = 1000


class TextRenderError(ValueError):
    """The text cannot be rendered here; print it through CUPS instead."""


@dataclass(frozen=True)
class _Font:
    data: bytes
    name: str
    units_per_em: int
    advance: int
    ascent: int
    descent: int
    bbox: tuple[int, int, int, int]
    starts: list[int]
    ends: list[int]
    # Per range: glyph id of the range start (format 12) or a per-character table.
    glyphs: list[object]

    def glyph(self, code_point: int) -> int:
        index = bisect.bisect_right(self.starts, code_point) - 1
        if index < 0 or code_point > self.ends[index]:
            return 0
        mapping = self.glyphs[index]
        if isinstance(mapping, int):
            return mapping + code_point - self.starts[index]
        return mapping.get(code_point, 0)  # type: ignore[union-attr]


def find_text_font() -> Path | None:
    configured = os.getenv("TEXT_FONT_PATH", "").strip()
    candidates = (configured,) if configured else FONT_CANDIDATES
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return path
    return None


def render_text_pdf(source: Path, destination: Path, font_path: Path) -> int:
    """Write the text as a PDF and return its page count."""
    font = _load_font(font_path)
    text = source.read_bytes().decode("utf-8-sig", errors="replace")
    pages = _paginate(text, int((PAGE_WIDTH - 2 * MARGIN) / (FONT_SIZE * font.advance / font.units_per_em)),
                      int((PAGE_HEIGHT - 2 * MARGIN) / LEADING))
    destination.write_bytes(_pdf(pages, font))
    return len(pages)


def _paginate(text: str, columns: int, rows: int) -> list[list[str]]:
    pages: list[list[str]] = [[]]

    def new_page() -> None:
        if len(pages) >= MAX_PAGES:
            raise TextRenderError(f"Text is longer than {MAX_PAGES} pages")
        pages.append([])

    for block in text.replace("\r\n", "\n").replace("\r", "\n").split("\f"):
        if pages[-1]:
            new_page()
        lines = block.split("\n")
        if len(lines) > 1 and lines[-1] == "":
            lines.pop()  # a final newline ends the last line
        for raw_line in lines:
            for row in _wrap(_expand_tabs(raw_line), columns):
                if len(pages[-1]) == rows:
                    new_page()
                pages[-1].append(row)
    # A trailing newline does not start an extra blank page.
    while len(pages) > 1 and not any(pages[-1]):
        pages.pop()
    return pages


def _wrap(line: str, columns: int) -> list[str]:
    """Break at the last space that fits, as a browser does; split only
    words longer than a whole line."""
    rows = []
    while len(line) > columns:
        cut = line.rfind(" ", 0, columns + 1)
        if cut <= 0:
            rows.append(line[:columns])
            line = line[columns:]
        else:
            rows.append(line[:cut])
            line = line[cut + 1:]
    rows.append(line)
    return rows


def _expand_tabs(line: str) -> str:
    out: list[str] = []
    column = 0
    for character in line:
        if character == "\t":
            width = TAB_SIZE - column % TAB_SIZE
            out.append(" " * width)
            column += width
        elif character >= " ":
            out.append(character)
            column += 1
    return "".join(out)


def _load_font(path: Path) -> _Font:
    data = path.read_bytes()
    try:
        tables = {}
        for index in range(struct.unpack(">H", data[4:6])[0]):
            tag, _, offset, length = struct.unpack(">4sIII", data[12 + 16 * index:28 + 16 * index])
            tables[tag.decode("latin-1")] = data[offset:offset + length]
        if "glyf" not in tables:
            raise TextRenderError(f"{path.name} is not a TrueType outline font")
        head, hhea, hmtx = tables["head"], tables["hhea"], tables["hmtx"]
        units_per_em = struct.unpack(">H", head[18:20])[0]
        bbox = struct.unpack(">hhhh", head[36:44])
        ascent, descent = struct.unpack(">hh", hhea[4:8])
        starts, ends, glyphs = _parse_cmap(tables["cmap"])
        font = _Font(data, path.stem.replace(" ", ""), units_per_em, 0, ascent, descent,
                     bbox, starts, ends, glyphs)
        metrics = struct.unpack(">H", hhea[34:36])[0]
        glyph = min(font.glyph(ord("0")), metrics - 1)
        advance = struct.unpack(">H", hmtx[4 * glyph:4 * glyph + 2])[0]
    except (struct.error, KeyError, IndexError) as exc:
        raise TextRenderError(f"Cannot read font {path.name}: {exc}") from exc
    return _Font(data, font.name, units_per_em, advance, ascent, descent, bbox, starts, ends, glyphs)


def _parse_cmap(cmap: bytes) -> tuple[list[int], list[int], list[object]]:
    records = {}
    for index in range(struct.unpack(">H", cmap[2:4])[0]):
        platform, encoding, offset = struct.unpack(">HHI", cmap[4 + 8 * index:12 + 8 * index])
        records[(platform, encoding)] = offset
    for key in ((3, 10), (0, 4), (3, 1), (0, 3)):
        if key not in records:
            continue
        table = cmap[records[key]:]
        if struct.unpack(">H", table[:2])[0] == 12:
            groups = [struct.unpack(">III", table[16 + 12 * i:28 + 12 * i])
                      for i in range(struct.unpack(">I", table[12:16])[0])]
            return [g[0] for g in groups], [g[1] for g in groups], [g[2] for g in groups]
        if struct.unpack(">H", table[:2])[0] == 4:
            return _parse_format4(table)
    raise TextRenderError("Font has no Unicode character map")


def _parse_format4(table: bytes) -> tuple[list[int], list[int], list[object]]:
    segments = struct.unpack(">H", table[6:8])[0] // 2
    end_codes = struct.unpack(f">{segments}H", table[14:14 + 2 * segments])
    base = 16 + 2 * segments
    start_codes = struct.unpack(f">{segments}H", table[base:base + 2 * segments])
    deltas = struct.unpack(f">{segments}h", table[base + 2 * segments:base + 4 * segments])
    range_base = base + 4 * segments
    range_offsets = struct.unpack(f">{segments}H", table[range_base:range_base + 2 * segments])
    starts, ends, glyphs = [], [], []
    for index in range(segments):
        start, end = start_codes[index], end_codes[index]
        if start == 0xFFFF:
            continue
        mapping: dict[int, int] = {}
        for code in range(start, end + 1):
            if range_offsets[index] == 0:
                glyph = (code + deltas[index]) & 0xFFFF
            else:
                position = range_base + 2 * index + range_offsets[index] + 2 * (code - start)
                glyph = struct.unpack(">H", table[position:position + 2])[0]
                glyph = (glyph + deltas[index]) & 0xFFFF if glyph else 0
            mapping[code] = glyph
        starts.append(start)
        ends.append(end)
        glyphs.append(mapping)
    return starts, ends, glyphs


def _pdf(pages: list[list[str]], font: _Font) -> bytes:
    scale = 1000 / font.units_per_em
    used: dict[int, str] = {}
    objects: list[bytes] = [b"", b""]

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    def stream(content: bytes, extra: str = "") -> bytes:
        packed = zlib.compress(content)
        return f"<< /Length {len(packed)} /Filter /FlateDecode {extra}>>\nstream\n".encode() + packed + b"\nendstream"

    font_file = add(stream(font.data, f"/Length1 {len(font.data)} "))
    descriptor = add((
        f"<< /Type /FontDescriptor /FontName /{font.name} /Flags 33 "
        f"/FontBBox [{' '.join(str(round(v * scale)) for v in font.bbox)}] /ItalicAngle 0 "
        f"/Ascent {round(font.ascent * scale)} /Descent {round(font.descent * scale)} "
        f"/CapHeight {round(font.ascent * scale)} /StemV 80 /FontFile2 {font_file} 0 R >>"
    ).encode())
    cid_font = add((
        f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{font.name} "
        f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
        f"/FontDescriptor {descriptor} 0 R /DW {round(font.advance * scale)} /CIDToGIDMap /Identity >>"
    ).encode())
    # Filled in after the pages, once the glyphs in use are known.
    type0 = add(b"")
    to_unicode = add(b"")
    page_ids = []
    for page in pages:
        lines = [f"BT /F1 {FONT_SIZE:g} Tf {LEADING:g} TL {MARGIN:.2f} {PAGE_HEIGHT - MARGIN - FONT_SIZE:.2f} Td"]
        for line in page:
            glyphs = []
            for character in line:
                glyph = font.glyph(ord(character))
                used.setdefault(glyph, character)
                glyphs.append(f"{glyph:04X}")
            lines.append(f"<{''.join(glyphs)}> Tj T*")
        lines.append("ET")
        content = add(stream("\n".join(lines).encode()))
        page_ids.append(add((
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 {type0} 0 R >> >> /Contents {content} 0 R >>"
        ).encode()))
    objects[to_unicode - 1] = stream(_to_unicode(used))
    objects[type0 - 1] = (
        f"<< /Type /Font /Subtype /Type0 /BaseFont /{font.name} /Encoding /Identity-H "
        f"/DescendantFonts [{cid_font} 0 R] /ToUnicode {to_unicode} 0 R >>"
    ).encode()
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    output += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    output += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(output)


def _to_unicode(used: dict[int, str]) -> bytes:
    entries = sorted(used.items())
    lines = [
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def /CMapType 2 def",
        "1 begincodespacerange <0000> <FFFF> endcodespacerange",
    ]
    for start in range(0, len(entries), 100):
        chunk = entries[start:start + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        lines.extend(f"<{glyph:04X}> <{character.encode('utf-16-be').hex().upper()}>" for glyph, character in chunk)
        lines.append("endbfchar")
    lines.append("endcmap CMapName currentdict /CMap defineresource pop end end")
    return "\n".join(lines).encode()
