"""Validate, render and index evidence-backed video research (no network calls)."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import html
import json
import math
import pathlib
import re
import sqlite3

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
KINDS = {"creator_view": "博主观点", "on_screen": "画面信息", "analyst_inference": "分析推断"}


def load(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def sha(path):
    digest = hashlib.sha256()
    with pathlib.Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate(analysis: dict, capture: pathlib.Path) -> list[dict]:
    if analysis.get("schema_version") != 1 or not analysis.get("videos"):
        raise ValueError("Expected schema_version=1 and at least one video")
    records = []
    seen = set()
    for video in analysis["videos"]:
        vid = video["video_id"]
        if not VIDEO_ID.fullmatch(vid) or vid in seen:
            raise ValueError("Invalid or duplicate video ID")
        seen.add(vid)
        directory = capture / vid
        info = load(directory / f"{vid}.info.json")
        if (info.get("id") != vid or info.get("availability") != "public"
                or info.get("live_status") not in {"not_live", "was_live"}
                or info.get("duration", 0) < 180):
            raise ValueError("Sample must be a verified public, completed long video")
        transcript = load(directory / "transcript.asr.json")
        if transcript.get("source") != "local_automatic_speech_recognition_not_creator_captions":
            raise ValueError("Unexpected transcript provenance")
        audio = directory / "media.f139.m4a"
        if sha(audio) != transcript.get("audio_sha256"):
            raise ValueError("Transcript does not match the captured audio")
        segments = transcript.get("segments", [])
        segment_map = {s["id"]: s for s in segments}
        if not segments or max(s["end"] for s in segments) < info["duration"] * 0.95:
            raise ValueError("Incomplete transcript: do not publish as full-video analysis")
        frame_map = {f["timestamp_seconds"]: f for f in load(directory / "frames.json")}
        if not video.get("claims") or not video.get("overview"):
            raise ValueError("An analysis needs an overview and evidence-linked claims")
        for claim in video["claims"]:
            if claim.get("kind") not in KINDS or not claim.get("text", "").strip():
                raise ValueError("Invalid claim kind or empty claim")
            timestamp = claim.get("timestamp_seconds")
            if (not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp)
                    or not 0 <= timestamp < info["duration"]):
                raise ValueError("Claim timestamp outside source video")
            refs = claim.get("evidence_segment_ids", [])
            if any(ref not in segment_map for ref in refs):
                raise ValueError("Unknown transcript evidence ID")
            if claim["kind"] != "on_screen" and not refs:
                raise ValueError("Spoken views/inferences need transcript evidence")
            if refs and not any(segment_map[ref]["start"] - 10 <= timestamp
                                <= segment_map[ref]["end"] + 10 for ref in refs):
                raise ValueError("Timestamp is not aligned to cited speech")
            frame_second = claim.get("frame_seconds")
            if claim["kind"] == "on_screen" and frame_second is None:
                raise ValueError("On-screen claims require a frame")
            if frame_second is not None:
                span_start = min((segment_map[ref]["start"] for ref in refs), default=timestamp)
                span_end = max((segment_map[ref]["end"] for ref in refs), default=timestamp)
                if frame_second not in frame_map or not span_start - 90 <= frame_second <= span_end + 90:
                    raise ValueError("Frame missing or not aligned with the claim")
                frame = frame_map[frame_second]
                relative = pathlib.Path(frame["path"])
                frame_path = (directory / relative).resolve()
                if relative.is_absolute() or not frame_path.is_relative_to(directory.resolve()):
                    raise ValueError("Frame path must stay inside its evidence directory")
                if sha(frame_path) != frame["sha256"]:
                    raise ValueError("Frame integrity mismatch")
        records.append({"analysis": video, "info": info, "transcript": transcript,
                        "frames": frame_map, "directory": directory})
    return records


def timestamp_link(vid, seconds):
    seconds = int(seconds)
    return f"https://www.youtube.com/watch?v={vid}&t={seconds}s"


def render(analysis, records):
    esc = html.escape
    parts = ["<!doctype html><html lang='zh-CN'><meta charset='utf-8'>",
             "<meta name='viewport' content='width=device-width,initial-scale=1'>",
             "<title>RhinoFinance 中文研究简报</title><style>",
             "body{font:17px/1.8 system-ui,sans-serif;background:#f1f5f9;color:#172033;margin:0}"
             "main{max-width:1060px;margin:auto;padding:32px 20px}section{background:white;padding:28px;"
             "border-radius:16px;margin:24px 0}h1{font-size:34px}h2{font-size:24px}"
             "a{color:#075985}img{width:100%;height:auto;border-radius:8px}"
             ".meta{font-size:14px;color:#526174}.tag{font-size:13px;background:#e0f2fe;padding:3px 8px;"
             "border-radius:4px}.claim{border-top:1px solid #dbe4ed;padding:20px 0}"
             "details{font-size:14px;color:#475569}nav a{display:block}"
             "@media print{section{break-inside:avoid}body{background:white}main{padding:0}}",
             "</style><main><h1>RhinoFinance · 中文研究简报</h1>",
             f"<p class='meta'>{esc(analysis['edition'])} · 样板 v{esc(str(analysis['revision']))}</p>",
             "<p>讲述依据为本地自动转写，关键画面由分析者核对。财经数据和观点均归属于原视频；"
             "本报告未独立核实公司公告或实时行情。完整音频、视频和自动转写随档案保存。</p>",
             f"<section><h2>三期速读</h2><p>{esc(analysis['summary'])}</p><nav>"]
    for record in records:
        vid = record["info"]["id"]
        parts.append(f"<a href='#{vid}'>{esc(record['info']['title'])}</a>")
    parts.append("</nav></section>")
    for record in records:
        video, info = record["analysis"], record["info"]
        vid = info["id"]
        parts.extend([f"<section id='{vid}'><h2>{esc(info['title'])}</h2>",
                      f"<p class='meta'>发布：{esc(video['published_shanghai'])}（北京时间） · "
                      f"时长 {info['duration']//60:02}:{info['duration']%60:02} · "
                      f"<a href='{timestamp_link(vid,0)}'>原视频</a></p>",
                      f"<p><strong>核心判断：</strong>{esc(video['overview'])}</p>"])
        for claim in video["claims"]:
            second = int(claim["timestamp_seconds"])
            parts.extend(["<article class='claim'>", f"<span class='tag'>{KINDS[claim['kind']]}</span> "
                          f"<a href='{timestamp_link(vid,second)}'>{second//60:02}:{second%60:02} ↗</a>",
                          f"<p>{esc(claim['text'])}</p>"])
            if claim.get("uncertainty"):
                parts.append(f"<p class='meta'>核对说明：{esc(claim['uncertainty'])}</p>")
            if claim.get("frame_seconds") is not None:
                frame = record["frames"][claim["frame_seconds"]]
                src = f"{vid}/{pathlib.Path(frame['path']).as_posix()}"
                parts.append(f"<a href='{timestamp_link(vid,claim['frame_seconds'])}'><img "
                             f"loading='lazy' src='{esc(src, quote=True)}' alt='关键画面 {claim['frame_seconds']} 秒'></a>")
            refs = claim.get("evidence_segment_ids", [])
            if refs:
                excerpt = " ".join(s["text"] for s in record["transcript"]["segments"] if s["id"] in refs)
                parts.append(f"<details><summary>查看对应自动转写（可能有识别错误）</summary>{esc(excerpt)}</details>")
            parts.append("</article>")
        parts.append(f"<h3>后续复盘</h3><p>{esc(video['review_questions'])}</p></section>")
    parts.append(f"<section><h2>跨期观察</h2><p>{esc(analysis['cross_video_observations'])}</p>"
                 "<p class='meta'>跨期观察是本报告的分析归纳，不等同于博主原话或预测胜率。"
                 "本样板未接入后续行情验证。</p></section></main></html>")
    return "\n".join(parts)


def render_markdown(analysis, records):
    lines = ["# RhinoFinance 中文研究简报", "", f"{analysis['edition']} · 样板 v{analysis['revision']}", "",
             "> 讲述来源：本地自动转写；关键画面经分析者核对。下述数据和观点归属于视频发布时的博主，"
             "未独立核实公司公告或实时行情。转写可能存在识别错误；条件、估值区间与实际交易须分别理解。", "",
             "## 三期速读", "", analysis["summary"], ""]
    for record in records:
        video, info = record["analysis"], record["info"]
        vid = info["id"]
        lines.extend([f"## {info['title']}", "", f"发布：{video['published_shanghai']}（北京时间） · "
                      f"时长 {info['duration']//60:02}:{info['duration']%60:02} · [原视频]({timestamp_link(vid,0)})", "",
                      f"**核心判断：** {video['overview']}", ""])
        for index, claim in enumerate(video["claims"], 1):
            second = int(claim["timestamp_seconds"])
            lines.extend([f"### {index}. {claim['headline']}", "",
                          f"**{KINDS[claim['kind']]}** · [{second//60:02}:{second%60:02}]({timestamp_link(vid,second)})", "",
                          claim["text"], ""])
            if claim.get("uncertainty"):
                lines.extend([f"> 核对说明：{claim['uncertainty']}", ""])
            if claim.get("frame_seconds") is not None:
                frame = record["frames"][claim["frame_seconds"]]
                src = f"{vid}/{pathlib.Path(frame['path']).as_posix()}"
                lines.extend([f"![关键画面：{claim['headline']}]({src})", "",
                              f"[回看画面对应时刻]({timestamp_link(vid,claim['frame_seconds'])})", ""])
        lines.extend(["### 后续复盘问题", "", video["review_questions"], ""])
    lines.extend(["## 跨期观察", "", analysis["cross_video_observations"], "",
                  "跨期观察属于本报告归纳，不等同于博主原话、预测胜率或实际交易收益。", "",
                  "## 原始材料与复盘", "",
                  "本地同目录保留 discovery.json、每条视频的元数据、音视频、transcript.asr.json、"
                  "transcript.asr.txt、frames.json 和关键画面；analysis-v*.json 保存逐条证据引用，"
                  "manifest-v*.json 保存文件校验值。飞书文档为阅读副本，本地材料是复盘依据。", ""])
    return "\n".join(lines)


def publish(analysis_path: pathlib.Path, database: pathlib.Path):
    capture = analysis_path.parent
    analysis = load(analysis_path)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", str(analysis.get("revision", ""))):
        raise ValueError("Invalid revision")
    records = validate(analysis, capture)
    revision = str(analysis["revision"])
    fingerprint = sha(analysis_path)
    sealed = capture / f"analysis-v{revision}.json"
    if sealed.exists() and sha(sealed) != fingerprint:
        raise ValueError("Analysis revision is immutable; use a new revision")
    sealed.write_bytes(analysis_path.read_bytes())
    report = capture / f"brief-v{revision}.html"
    report.write_text(render(analysis, records), encoding="utf-8")
    (capture / f"brief-v{revision}.md").write_text(render_markdown(analysis, records), encoding="utf-8")
    artifacts = []
    for path in sorted(capture.rglob("*")):
        if path.is_file() and path.name != "analysis.json" and not path.name.startswith("manifest"):
            artifacts.append({"path": path.relative_to(capture).as_posix(), "bytes": path.stat().st_size,
                              "sha256": sha(path)})
    manifest = {"schema_version": 1, "analysis_sha256": fingerprint,
                "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "artifacts": artifacts}
    (capture / f"manifest-v{revision}.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    database.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE IF NOT EXISTS analyses (video_id TEXT, revision TEXT, source TEXT, "
                           "title TEXT, payload TEXT, analysis_sha256 TEXT, PRIMARY KEY(video_id, revision))")
        connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS research_fts USING fts5(video_id UNINDEXED, "
                           "revision UNINDEXED, body, tokenize='trigram')")
        for record in records:
            vid = record["info"]["id"]
            payload = json.dumps(record["analysis"], ensure_ascii=False)
            existing = connection.execute("SELECT analysis_sha256 FROM analyses WHERE video_id=? AND revision=?",
                                          (vid, revision)).fetchone()
            if existing and existing[0] != fingerprint:
                raise ValueError("Database revision collision")
            connection.execute("INSERT OR IGNORE INTO analyses VALUES (?,?,?,?,?,?)", (
                vid, revision, str(capture.resolve()), record["info"]["title"], payload, fingerprint))
            connection.execute("DELETE FROM research_fts WHERE video_id=? AND revision=?", (vid, revision))
            transcript = " ".join(s["text"] for s in record["transcript"]["segments"])
            connection.execute("INSERT INTO research_fts VALUES (?,?,?)", (vid, revision,
                               record["info"]["title"] + "\n" + payload + "\n" + transcript))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("analysis", type=pathlib.Path, nargs="?")
    parser.add_argument("--query", help="Search saved titles, analysis and transcripts (literal substring)")
    parser.add_argument("--database", type=pathlib.Path, default=pathlib.Path("research-data/library.sqlite"))
    args = parser.parse_args()
    if args.query:
        if not args.database.is_file():
            parser.error("Research database does not exist")
        with contextlib.closing(sqlite3.connect(f"{args.database.resolve().as_uri()}?mode=ro", uri=True)) as connection:
            rows = connection.execute(
                "SELECT a.video_id,a.revision,a.title,a.source FROM analyses a JOIN research_fts f "
                "ON a.video_id=f.video_id AND a.revision=f.revision WHERE instr(lower(f.body),lower(?))>0",
                (args.query,),
            ).fetchall()
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif args.analysis:
        print(publish(args.analysis, args.database))
    else:
        parser.error("Provide an analysis JSON or --query")


if __name__ == "__main__":
    main()
