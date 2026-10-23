"""Extract timestamped evidence frames and contact sheets from captured media."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=pathlib.Path)
    parser.add_argument("--interval", type=int, default=45)
    args = parser.parse_args()
    if args.interval < 5:
        parser.error("interval must be at least 5 seconds")
    import imageio_ffmpeg
    from PIL import Image, ImageDraw

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    for info_path in args.capture.glob("*/*.info.json"):
        directory = info_path.parent
        info = json.loads(info_path.read_text(encoding="utf-8"))
        video = directory / "media.f136.mp4"
        frames = directory / "frames"
        frames.mkdir(exist_ok=True)
        manifest = []
        for second in range(10, int(info["duration"]), args.interval):
            output = frames / f"frame-{second:05}.jpg"
            if not output.exists():
                subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", str(second),
                                "-i", str(video), "-frames:v", "1", "-q:v", "2", str(output)],
                               check=True, capture_output=True, timeout=30)
            manifest.append({"timestamp_seconds": second, "path": str(output.relative_to(directory)),
                             "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                             "source_url": f"https://www.youtube.com/watch?v={info['id']}&t={second}s"})
        for chunk_start in range(0, len(manifest), 20):
            chunk = manifest[chunk_start:chunk_start + 20]
            sheet = Image.new("RGB", (1536, 244 * ((len(chunk) + 3) // 4)), "#111827")
            draw = ImageDraw.Draw(sheet)
            for index, frame in enumerate(chunk):
                image = Image.open(directory / frame["path"])
                image.thumbnail((384, 216))
                x, y = (index % 4) * 384, (index // 4) * 244
                sheet.paste(image, (x, y))
                sec = frame["timestamp_seconds"]
                draw.text((x + 8, y + 220), f"{sec//60:02}:{sec%60:02} | t={sec}s", fill="white")
            sheet.save(directory / f"contact-{chunk_start//20+1:02}.jpg", quality=90)
        (directory / "frames.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{info['id']}: {len(manifest)} timestamped frames", flush=True)


if __name__ == "__main__":
    main()
