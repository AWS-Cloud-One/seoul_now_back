"""Read official XLSX/SHP files without database access or fixed job paths."""

import hashlib
import re
import struct
import zipfile
from io import BytesIO
from xml.etree import ElementTree as ET


def read_xlsx_rows(source):
    """Read sheet1, preserving sparse column letters and source row order."""
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(source) as book:
        strings = (
            ["".join(node.itertext()) for node in ET.fromstring(book.read("xl/sharedStrings.xml"))]
            if "xl/sharedStrings.xml" in book.namelist()
            else []
        )
        sheet = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
        rows = []
        for row in sheet.findall(".//s:row", ns):
            values = {}
            for cell in row.findall("s:c", ns):
                node = cell.find("s:is" if cell.get("t") == "inlineStr" else "s:v", ns)
                if node is None:
                    continue
                value = "".join(node.itertext()) if cell.get("t") == "inlineStr" else node.text
                values[re.sub(r"\d+", "", cell.get("r"))] = (
                    strings[int(value)] if cell.get("t") == "s" else value
                )
            rows.append(values)
        return rows


def read_topis_files(vertices_path, mapping_path):
    # Hash the same bytes that were parsed, even if the file is replaced during import.
    vertices = vertices_path.read_bytes()
    return {
        "vertices": read_xlsx_rows(BytesIO(vertices))[1:],
        "mapping": read_xlsx_rows(mapping_path)[1:],
        "source_sha256": hashlib.sha256(vertices).hexdigest(),
    }


def read_shapes(path):
    """Read the official 121-place WGS84 polygon archive, retaining every ring."""
    with zipfile.ZipFile(path) as archive:

        def read(suffix):
            return archive.read(next(n for n in archive.namelist() if n.endswith(suffix)))

        if "WGS_1984" not in read(".prj").decode():
            raise ValueError("unexpected_crs")
        dbf, shp = read(".dbf"), read(".shp")
        header, size = struct.unpack_from("<HH", dbf, 8)
        count = struct.unpack_from("<I", dbf, 4)[0]
        fields = [
            (dbf[o : o + 11].split(b"\0")[0].decode(), dbf[o + 16])
            for o in range(32, header - 1, 32)
        ]
        records = []
        for i in range(count):
            row = dbf[header + i * size : header + (i + 1) * size]
            values = {}
            cursor = 1
            for name, length in fields:
                values[name] = row[cursor : cursor + length].decode("utf-8").strip()
                cursor += length
            records.append(values)
        offset = 100
        shapes = []
        while offset < len(shp):
            length = struct.unpack_from(">i", shp, offset + 4)[0] * 2
            body = shp[offset + 8 : offset + 8 + length]
            offset += 8 + length
            if struct.unpack_from("<i", body)[0] != 5:
                raise ValueError("expected_polygon")
            parts, points = struct.unpack_from("<ii", body, 36)
            starts = list(struct.unpack_from(f"<{parts}i", body, 44)) + [points]
            xy = [struct.unpack_from("<dd", body, 44 + parts * 4 + i * 16) for i in range(points)]
            if any(not (126 <= x <= 128 and 37 <= y <= 38) for x, y in xy):
                raise ValueError("invalid_coordinates")
            rings = [xy[starts[i] : starts[i + 1]] for i in range(parts)]
            if any(len(ring) < 4 or ring[0] != ring[-1] for ring in rings):
                raise ValueError("invalid_ring")
            shapes.append(rings)
        if len(records) != 121 or len(shapes) != 121 or len({r["AREA_CD"] for r in records}) != 121:
            raise ValueError("incomplete_official_places")
        return list(zip(records, shapes, strict=True))
