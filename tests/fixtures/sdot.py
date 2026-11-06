"""Synthetic S-DoT rows, distinct from archived provider samples."""

import hashlib
from io import BytesIO
from xml.etree.ElementTree import Element, SubElement, tostring
from zipfile import ZipFile, ZipInfo

RECEIVED = "2026-11-04T01:00:00+00:00"
HEADER = {
    "A": "순번",
    "B": "방문자 센서코드",
    "C": "시리얼번호",
    "D": "주소",
    "E": "위도",
    "F": "경도",
}


def location(code="1", **updates):
    return {"B": code, "C": "device-" + code, "D": "서울특별시", "E": "37.5", "F": "127", **updates}


def sensor_file(rows=None, *, complete=True, reference_date="2025-11-13"):
    rows = rows if rows is not None else [location()]
    root = Element("worksheet", xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main")
    sheet = SubElement(root, "sheetData")
    for index, values in enumerate([HEADER, *rows], 1):
        row = SubElement(sheet, "row", r=str(index))
        for column, value in values.items():
            cell = SubElement(row, "c", r=f"{column}{index}", t="inlineStr")
            SubElement(SubElement(cell, "is"), "t").text = str(value)
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(
            ZipInfo("xl/worksheets/sheet1.xml", date_time=(2000, 1, 1, 0, 0, 0)), tostring(root)
        )
    body = buffer.getvalue()
    return {
        "header": HEADER,
        "rows": rows,
        "filename": "synthetic.xlsx",
        "sha256": hashlib.sha256(body).hexdigest(),
        "byte_size": len(body),
        "body": body,
        "reference_date": reference_date,
        "complete": complete,
    }


def observation(code="1", **updates):
    return {
        "SERIAL": code,
        "SENSING_TIME": "2026-11-04_09:00:00",
        "DATE": "2026-11-04 09:01:00.0",
        "VISITOR_COUNT": "0",
        "DATA_NO": "1",
        "MODELNAME": "test",
        **updates,
    }


def collected(rows, receipt=RECEIVED):
    return {
        "rows": rows,
        "row_contexts": [{"page": 0, "row": i, "received_at": receipt} for i in range(len(rows))],
        "provider_total": len(rows),
        "pages": [
            {
                "start": 1,
                "end": 1000,
                "received": len(rows),
                "received_at": receipt,
                "body": {
                    "sDoTPeople": {
                        "RESULT": {"CODE": "INFO-000"},
                        "list_total_count": len(rows),
                        "row": rows,
                    }
                },
            }
        ],
        "issues": [],
        "query": {"start": 1, "end": 10000, "date_filter": None},
        "query_complete": True,
    }
