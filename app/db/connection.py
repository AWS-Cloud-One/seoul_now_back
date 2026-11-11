"""Shared psycopg connection policy for API, jobs and Alembic."""

import ssl
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.engine import make_url

if TYPE_CHECKING:
    from app.core.config import Settings

SSL_MODES = frozenset({"disable", "allow", "prefer", "require", "verify-ca", "verify-full"})
AWS_QUERY_KEYS = frozenset({"sslmode", "sslrootcert", "gssencmode", "application_name"})


def connection_args(settings: Settings) -> dict[str, str | int]:
    url = make_url(settings.database_url.get_secret_value())
    mode = settings.db_sslmode
    root = str(settings.db_sslrootcert) if settings.db_sslrootcert is not None else None
    aws = settings.runtime_env == "aws"
    if aws:
        if mode != "verify-full" or root is None:
            raise ValueError("AWS requires DB_SSLMODE=verify-full and DB_SSLROOTCERT")
        if not url.host or url.host.startswith(("/", "@")) or "," in url.host:
            raise ValueError("AWS requires a single TCP database host")
        # Reject alternate routing/service files and TLS options with ambiguous precedence.
        if url.query.keys() - AWS_QUERY_KEYS:
            raise ValueError("Unsupported AWS database URL query option")

    for key, explicit in (("sslmode", mode), ("sslrootcert", root)):
        value = url.query.get(key)
        if value is not None and (
            not isinstance(value, str) or (explicit is not None and value != explicit)
        ):
            raise ValueError("Conflicting or repeated database TLS option")
    mode = mode if mode is not None else url.query.get("sslmode")
    root = root if root is not None else url.query.get("sslrootcert")
    if mode is not None and mode not in SSL_MODES:
        raise ValueError("Invalid database TLS mode")
    if root is not None:
        path = Path(root)
        if aws and not path.is_absolute():
            raise ValueError("AWS DB_SSLROOTCERT must be an absolute file path")
        try:
            if not path.is_file():
                raise ValueError("CA must be a regular file")
            # Parse the supplied file, without implicitly using the OS trust store.
            ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cafile=path)
        except OSError, ValueError:
            raise ValueError(
                "Database CA file must be readable and contain PEM certificates"
            ) from None

    args: dict[str, str | int] = {"connect_timeout": settings.db_connect_timeout_seconds}
    if mode is not None:
        args["sslmode"] = mode
    if root is not None:
        args["sslrootcert"] = root
    if aws:
        # libpq can prefer GSS encryption even with verify-full; require TLS explicitly.
        if url.query.get("gssencmode", "disable") != "disable":
            raise ValueError("AWS database connections require gssencmode=disable")
        args["gssencmode"] = "disable"
    return args
