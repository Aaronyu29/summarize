"""Produce a single-video brief from a validated, reviewed local analysis archive.

Accepts a YouTube watch URL. An unknown video fails explicitly rather than reusing
another video's summary. New videos still need capture, transcription and review.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import pathlib
import re
import urllib.parse

from finance_archive import render, render_markdown, sha, validate


def video_id(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"www.youtube.com", "youtube.com", "youtu.be"}:
        raise ValueError("Expected an HTTPS YouTube video link")
    value = parsed.path.strip("/") if parsed.hostname == "youtu.be" else urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
    if parsed.hostname != "youtu.be" and parsed.path != "/watch":
        raise ValueError("Expected a watch URL, not a channel or playlist")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        raise ValueError("Invalid video ID")
    return value


def create(url, archive):
    archive = pathlib.Path(archive)
    source_path = archive / "analysis-v1.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    vid = video_id(url)
    selected = [video for video in source["videos"] if video["video_id"] == vid]
    if len(selected) != 1:
        raise ValueError("No reviewed analysis for this exact video; capture and review it first")
    analysis = copy.deepcopy(source)
    analysis["videos"] = selected
    analysis["edition"] = f"单期视频研究样板 · {selected[0]['published_shanghai']}（北京时间发布）"
    analysis["summary"] = selected[0]["overview"]
    analysis["cross_video_observations"] = (
        "本期复盘要把三件事分开：财报已经披露的数字、作者对未来支出和估值的假设、真正发生的交易。"
        "例如 TSLA 现金流改善并不自动证明经营趋势反转，META 未给具体 2027 年支出金额也不能被写成已削减资本支出。"
        "市场层面应分别记录向下破位和向上挤压的触发条件，后续再对照行情核验；当前不计算命中率或实际收益。")
    analysis["review_provenance"] = {
        "mode": "assistant_review_of_full_transcript_and_keyframes",
        "parent_analysis_sha256": sha(source_path),
        "source_url": f"https://www.youtube.com/watch?v={vid}",
        "not_an_unattended_llm_generator": True,
    }
    records = validate(analysis, archive)
    stem = f"reviewed-{vid}-v1"
    outputs = {
        f"{stem}.json": json.dumps(analysis, ensure_ascii=False, indent=2),
        f"{stem}.md": render_markdown(analysis, records).replace("## 三期速读", "## 本期速读").replace("## 跨期观察", "## 分析补充"),
        f"{stem}.html": render(analysis, records).replace("<h2>三期速读</h2>", "<h2>本期速读</h2>").replace("<h2>跨期观察</h2>", "<h2>分析补充</h2>"),
    }
    for name, content in outputs.items():
        path = archive / name
        if path.exists() and path.read_text(encoding="utf-8") != content:
            raise ValueError("An existing reviewed revision cannot be overwritten; create a new revision")
        path.write_text(content, encoding="utf-8")
    evidence = [{"path": name, "sha256": sha(archive / name)} for name in outputs]
    manifest = {"video_id": vid, "source_url": analysis["review_provenance"]["source_url"],
                "parent_analysis_sha256": sha(source_path), "artifacts": evidence,
                "raw_evidence_manifest": "manifest-v1.json", "points": len(selected[0]["claims"]),
                "selected_images": sum(claim.get("frame_seconds") is not None for claim in selected[0]["claims"])}
    (archive / f"{stem}.manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return archive / f"{stem}.md"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--archive", required=True, type=pathlib.Path)
    args = parser.parse_args()
    print(create(args.url, args.archive))


if __name__ == "__main__":
    main()
