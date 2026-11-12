import asyncio

import httpx
import pytest

from app.clients.seoul import CollectionError
from app.modules.data_pipeline.collectors.road_names import collect_road_name

LINK = "1220003800"


def document(link=LINK, name=" 분당수서로 ", *, duplicate=False):
    row = f"<row><link_id>{link}</link_id><road_name>{name}</road_name></row>"
    return (
        f"<LinkInfo><list_total_count>{2 if duplicate else 1}</list_total_count>"
        f"<RESULT><CODE>INFO-000</CODE></RESULT>{row * (2 if duplicate else 1)}</LinkInfo>"
    )


def test_link_info_uses_requested_id_and_returns_only_confirmed_name():
    def handler(request):
        assert request.url.path.endswith(f"/xml/LinkInfo/1/5/{LINK}")
        return httpx.Response(200, text=document())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await collect_road_name(client, "test", LINK) == {
                "link_id": LINK,
                "name": "분당수서로",
            }

    asyncio.run(run())


@pytest.mark.parametrize("body", [document(link="1000000000"), document(duplicate=True)])
def test_unmatched_or_ambiguous_name_is_rejected(body):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))
        ) as client:
            with pytest.raises(CollectionError, match="^invalid_road_name_response$"):
                await collect_road_name(client, "test", LINK)

    asyncio.run(run())


def test_provider_auth_failure_does_not_expose_key_or_response():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(403, text=str(request.url))
            )
        ) as client:
            with pytest.raises(CollectionError, match="^provider_auth_error$"):
                await collect_road_name(client, "private-test-key", LINK)

    asyncio.run(run())
