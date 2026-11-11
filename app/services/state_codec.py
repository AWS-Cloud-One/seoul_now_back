"""JSON conversion for temporary state shared through PostgreSQL.

Only explicit, well-known value types are encoded. Unknown objects fail loudly
instead of being pickled, so database content is never executed or trusted as code.
"""

import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from pydantic import BaseModel


def _default(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal | UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, set | frozenset):
        return sorted(value, key=str)
    raise TypeError(f"Unsupported state value: {type(value).__name__}")


def to_json(value):
    """Return plain JSON data (dict/list/str/number/bool/None) for a JSONB column.

    Tuples become lists, so comparisons must use :func:`to_json` on both sides.
    """
    return json.loads(json.dumps(value, default=_default, ensure_ascii=False, allow_nan=False))
