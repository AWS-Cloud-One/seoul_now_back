"""Register SQLAlchemy models here so Alembic can discover their metadata.

Import each new model below and add its name to __all__.
Application tables will be defined when their requirements are agreed.
"""

from app.db.base import Base

__all__ = ["Base"]
