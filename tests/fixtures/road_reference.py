"""Synthetic reference evidence for unrelated integration tests; not provider validation."""

from app.modules.data_pipeline.identity import fingerprint
from app.modules.data_pipeline.pipelines.snapshots import save_snapshot


def road_candidate(data, *, version="2025", mapping_version="2025-03-06", complete=True):
    return {
        **data,
        "reference": {
            "reference_id": fingerprint(data),
            "complete": complete,
            "allowed_exclusions": [],
            "source_crs": "EPSG:5181",
            "inputs": {
                "vertices": {"version": version, "sha256": "1" * 64, "reference_date": None},
                "mapping": {
                    "version": mapping_version,
                    "sha256": "2" * 64,
                    "reference_date": "2025-03-06",
                },
            },
        },
        "report": {
            "file_links": len(data.get("features", [])),
            "spatial_validation": {
                "status": "checked_available_references",
                "error_code": None,
                "evidence_source": "synthetic_fixture_for_unrelated_storage_tests",
            },
        },
    }


async def save_fixture_snapshot(session, source, data, **kwargs):
    if source.startswith("topis_geometry:"):
        data = road_candidate(data, version=source.split(":", 1)[1])
    return await save_snapshot(session, source, data, **kwargs)
