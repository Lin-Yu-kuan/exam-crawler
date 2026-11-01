"""TLS 診斷：僅觀察憑證鏈，不繞過驗證、不發送大量請求。

用途：判斷 CERTIFICATE_VERIFY_FAILED 是本機根憑證不足、站點未送中介憑證，
或連線被中介 Proxy 攔截所致。
"""

from __future__ import annotations

import socket
import ssl
import sys

HOSTS = ["wwwc.moex.gov.tw", "www.moex.gov.tw", "www.gov.tw", "example.com"]


def probe(host: str) -> None:
    print(f"=== {host} ===")
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, 443), timeout=15) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert()
                print("verify: OK")
                print("subject:", cert.get("subject"))
                print("issuer:", cert.get("issuer"))
                print("notAfter:", cert.get("notAfter"))
    except ssl.SSLCertVerificationError as exc:
        print("verify: FAILED ->", exc.verify_message or exc)
        # 只取憑證內容作為診斷，不建立信任連線
        try:
            unverified = ssl._create_unverified_context()
            with socket.create_connection((host, 443), timeout=15) as raw:
                with unverified.wrap_socket(raw, server_hostname=host) as tls:
                    der = tls.getpeercert(binary_form=True)
                    print("peer cert DER bytes:", len(der) if der else 0)
                    info = tls._sslobj.get_unverified_chain()  # type: ignore[attr-defined]
                    for idx, c in enumerate(info or []):
                        name = {v: k for k, v in c.items()}
                        print(f"  chain[{idx}] subject:", name.get("subject"))
                        print(f"  chain[{idx}] issuer :", name.get("issuer"))
        except Exception as inner:  # 診斷用途
            print("chain probe failed:", type(inner).__name__, inner)
    except OSError as exc:
        print("connection error:", type(exc).__name__, exc)
    print()


def main() -> int:
    print("python:", sys.version)
    print("openssl:", ssl.OPENSSL_VERSION)
    print("default verify paths:", ssl.get_default_verify_paths())
    print()
    for host in HOSTS:
        probe(host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
