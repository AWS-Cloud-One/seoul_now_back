"""Read-only extensions over the existing Kakao transport; no AI or ranking."""

from app.clients.kakao import kakao_get


class KakaoQueryClient:
    def __init__(self, client):
        self.client = client

    async def places(self, query, page, *, center=None, radius=None, category=None):
        params = {"query": query, "page": page, "size": 15}
        if center is not None:
            params.update(x=center.longitude, y=center.latitude, radius=radius, sort="distance")
        if category is not None:
            params["category_group_code"] = category
        return await kakao_get(
            self.client.http,
            "https://dapi.kakao.com/v2/local/search/keyword.json",
            params,
            self.client.key,
        )

    async def driving(self, origin, destination, *, include_geometry):
        return await kakao_get(
            self.client.http,
            "https://apis-navi.kakaomobility.com/v1/directions",
            {
                "origin": f"{origin.longitude},{origin.latitude}",
                "destination": f"{destination.longitude},{destination.latitude}",
                "summary": "false" if include_geometry else "true",
                "priority": "RECOMMEND",
            },
            self.client.key,
        )
