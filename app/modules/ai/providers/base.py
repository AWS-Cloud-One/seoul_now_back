"""Shared structured response contract for selectable model providers."""

from typing import Protocol

from pydantic import BaseModel


class AIProvider(Protocol):
    async def complete[T: BaseModel](
        self, instructions: str, payload: dict, output_type: type[T]
    ) -> T: ...
