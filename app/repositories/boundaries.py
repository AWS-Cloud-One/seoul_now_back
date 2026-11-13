"""Atomic SGIS boundary and road-relation writes from validated current features."""

from datetime import UTC, datetime

from app.models import Base
from app.modules.data_pipeline.identity import PROCESSOR_VERSION, fingerprint
from app.modules.data_pipeline.normalizers.storage_fields import (
    administrative_area_fields,
    region_boundary_fields,
)
from app.repositories.normalization import NormalizedWriter
from app.repositories.road_reference import lock_road_reference, replace_administrative_areas


def store_boundaries(session, data, boundary):
    writer = NormalizedWriter(session, Base.metadata)
    lock_road_reference(session)
    common = dict(
        processed_at=datetime.now(UTC), processor_version=PROCESSOR_VERSION, is_active=True
    )
    writer.source("sgis_areas")
    writer.source("sgis_seoul_boundary")
    for feature in boundary["features"]:
        writer.upsert_geometry(
            "region_boundaries",
            dict(
                **region_boundary_fields(feature, fingerprint(feature), boundary["boundary_year"]),
                source="sgis_seoul_boundary",
                **common,
            ),
            ["code_system", "region_code"],
        )
    for feature in data.get("district_features", []):
        version = fingerprint(feature)
        writer.upsert_geometry(
            "region_boundaries",
            dict(
                code_system="sgis",
                region_code=feature["id"],
                name=feature["properties"]["name"],
                geometry=feature["geometry"],
                boundary_version=version,
                source="sgis_areas",
                source_version=None,
                **common,
            ),
            ["code_system", "region_code"],
        )
    values = [
        dict(
            **administrative_area_fields(feature, fingerprint(feature)),
            source="sgis_areas",
            **common,
        )
        for feature in data["features"]
    ]
    replace_administrative_areas(writer, values, complete=data["complete"])
