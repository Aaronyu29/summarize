"""Capture public channel evidence for a reproducible video research sample.

This collector never generates an analysis from a title/description fallback.
Network operations are explicit; existing raw snapshots are not overwritten.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import urllib.request
import xml.etree.ElementTree as ET

CHANNEL_ID = "UCFQsi7WaF5X41tcuOryDk8w"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


def parse_feed(raw: bytes) -> list[dict]:
    root = ET.fromstring(raw)
    videos = []
    for entry in root.findall("a:entry", NS):
        videos.append({
            "video_id": entry.findtext("yt:videoId", namespaces=NS),
            "channel_id": entry.findtext("yt:channelId", namespaces=NS),
            "title": entry.findtext("a:title", namespaces=NS),
            "published_utc": entry.findtext("a:published", namespaces=NS),
            "url": entry.find("a:link", NS).attrib["href"],
            "status": "discovered_not_yet_verified_public_complete",
        })
    return sorted(videos, key=lambda v: v["published_utc"], reverse=True)


def write_json(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--channel-id", default=CHANNEL_ID)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    feed_path = args.output / "channel-feed.xml"
    if feed_path.exists():
        raise SystemExit("Use a new output directory to preserve the existing capture.")
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={args.channel_id}"
    with urllib.request.urlopen(url, timeout=30) as response:
        raw = response.read()
    feed_path.write_bytes(raw)
    videos = parse_feed(raw)
    manifest = {
        "schema_version": 1,
        "captured_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_url": url,
        "feed_sha256": hashlib.sha256(raw).hexdigest(),
        "selection_policy": "Newest feed entries; verify public, finished, non-short before final selection",
        "entries": videos,
    }
    write_json(args.output / "discovery.json", manifest)
    print(json.dumps(videos[:5], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
