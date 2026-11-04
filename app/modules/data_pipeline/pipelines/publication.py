"""Commit-independent publication outcomes; collection results remain collection results."""

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services.map_publication import MapPublicationService, PublicationNotReady


async def publish_result(engine, result, *, geometry=(), observations=()):
    layers = {}
    for layer, mode in [(x, "geometry") for x in geometry] + [
        (x, "observations") for x in observations
    ]:
        if mode == "geometry" and result["status"] not in {"success", "partial"}:
            layers[layer] = {
                "status": "skipped",
                "reason_code": "storage_not_updated",
                "mode": mode,
            }
            continue
        try:
            async with async_sessionmaker(engine).begin() as session:
                publisher = MapPublicationService(session)
                if mode == "geometry":
                    count = await publisher.publish(layer)
                else:
                    try:
                        count = await publisher.refresh_observations(layer)
                    except PublicationNotReady as exc:
                        if exc.code != "geometry_publication_required":
                            raise
                        # Canonical geometry has already passed import validation. Recover
                        # a missing/failed publication atomically with its observations.
                        count = await publisher.publish(layer)
                        mode = "geometry"
                outcome = {
                    "status": "success",
                    "mode": mode,
                    "features": count,
                    "geometry_changed": publisher.geometry_changed,
                    "observations_changed": publisher.observations_changed,
                }
            layers[layer] = outcome  # Only report success after commit succeeds.
        except PublicationNotReady as exc:
            layers[layer] = {"status": "deferred", "reason_code": exc.code, "mode": mode}
        except Exception:
            # Do not turn a failed publication into a failed collection or expose DB/URL secrets.
            layers[layer] = {
                "status": "failed",
                "error_code": "map_publication_failed",
                "mode": mode,
            }
    return {**result, "publication": publication_summary(layers)}


def publication_summary(layers):
    states = {value["status"] for value in layers.values()}
    status = (
        "failed"
        if "failed" in states
        else "deferred"
        if "deferred" in states
        else "success"
        if "success" in states
        else "skipped"
    )
    return {"status": status, "layers": layers}
