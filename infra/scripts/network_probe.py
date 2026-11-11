"""Read-only network checks without AWS or database credentials."""

import argparse
import json
import socket
import ssl
import struct


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-host", required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args()
    context = ssl.create_default_context(cafile="/app/certs/rds/ap-northeast-2-bundle.pem")
    with socket.create_connection((args.db_host, 5432), timeout=5) as sock:
        # PostgreSQL SSLRequest precedes the TLS handshake; no authentication is sent.
        sock.sendall(struct.pack("!II", 8, 80877103))
        if sock.recv(1) != b"S":
            raise RuntimeError("postgresql_ssl_unavailable")
        with context.wrap_socket(sock, server_hostname=args.db_host) as encrypted:
            print(
                json.dumps(
                    {
                        "check": "rds_server_tls",
                        "protocol": encrypted.version(),
                        "hostname_verified": True,
                        "database_login_tested": False,
                    }
                ),
                flush=True,
            )
    for host, port, tls in [
        (f"secretsmanager.{args.region}.amazonaws.com", 443, True),
        (f"bedrock-runtime.{args.region}.amazonaws.com", 443, True),
        ("dapi.kakao.com", 443, True),
        ("apis-navi.kakaomobility.com", 443, True),
        ("openapi.seoul.go.kr", 8088, False),
    ]:
        with socket.create_connection((host, port), timeout=5) as sock:
            if tls:
                with ssl.create_default_context().wrap_socket(sock, server_hostname=host):
                    pass
        print(json.dumps({"host": host, "port": port, "connected": True}), flush=True)
    print(json.dumps({"check": "network_probe", "status": "pass"}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps({"check": "network_probe", "error_type": type(error).__name__}), flush=True
        )
        raise SystemExit(1) from None
