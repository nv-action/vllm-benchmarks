#!/usr/bin/env python3
"""Write task-private Maven proxy settings without changing the user's ~/.m2."""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlsplit


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: write_maven_settings.py OUTPUT")
    output = Path(sys.argv[1])
    root = ET.Element("settings", xmlns="http://maven.apache.org/SETTINGS/1.0.0")
    proxy_url = os.environ.get("PERF_PROXY", "").strip()
    if proxy_url:
        parsed = urlsplit(proxy_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
            raise ValueError("PERF_PROXY must be http[s]://host:port")
        proxies = ET.SubElement(root, "proxies")
        proxy = ET.SubElement(proxies, "proxy")
        for name, value in (
            ("id", "boostkit-http"), ("active", "true"),
            ("protocol", parsed.scheme), ("host", parsed.hostname),
            ("port", str(parsed.port)),
            ("nonProxyHosts", "localhost|127.0.0.1|[::1]"),
        ):
            ET.SubElement(proxy, name).text = value
        if parsed.username:
            ET.SubElement(proxy, "username").text = unquote(parsed.username)
        if parsed.password:
            ET.SubElement(proxy, "password").text = unquote(parsed.password)
    output.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)
    output.chmod(0o600)
    print("[rocksdb] task-private Maven settings prepared", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
