import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.clients.seoul import CollectionError
from app.modules.data_pipeline.collectors.topis import collect_topis
from jobs import collect_topis_network


def test_all_scope_does_not_keep_area_or_twenty_link_limit(monkeypatch):
    geometry = SimpleNamespace(
        data={"features": [{"id": "1000000300"}, {"id": "1220003800"}, {"id": "1000000400"}]}
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, model, key):
            assert key == "topis_geometry:2025"
            return geometry

    class Engine:
        disposed = False

        async def dispose(self):
            self.disposed = True

    engine = Engine()
    monkeypatch.setattr(collect_topis_network, "create_engine", lambda settings: engine)
    monkeypatch.setattr(
        collect_topis_network, "async_sessionmaker", lambda engine: lambda: Session()
    )
    links, report = asyncio.run(collect_topis_network.targets(None, 1))
    assert len(links) == 3
    assert report["selected_links"] == 3 and report["truncated"] is False
    assert engine.disposed


def test_quota_and_auth_errors_are_classified_without_key_leak():
    async def run():
        for code, expected in [
            ("ERROR-337", "provider_quota_exceeded"),
            ("INFO-100", "provider_auth_error"),
            ("INFO-200", "provider_no_observation"),
        ]:

            async def handler(request, code=code):
                return httpx.Response(200, text=f"<RESULT><CODE>{code}</CODE></RESULT>")

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with pytest.raises(CollectionError) as error:
                    await collect_topis(client, "secret-example-key", "1000000300")
                assert str(error.value) == expected
                assert "secret-example-key" not in str(error.value)

    asyncio.run(run())
