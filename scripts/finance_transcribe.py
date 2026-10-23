"""Bounded-memory ASR: decode one audio window at a time, then release it."""
from __future__ import annotations
import argparse
import datetime as dt
import gc
import hashlib
import json
import os
import pathlib
import subprocess
import time


def windows(duration, size=300, context=5):
    if size < 30 or duration <= 0 or context < 0:
        raise ValueError('Invalid audio window settings')
    start = 0
    while start < duration:
        end = min(start + size, duration)
        yield start, end, max(0, start - context), min(duration, end + context)
        start = end


def globalize(segments, core_start, core_end, read_start):
    """Assign overlap segments to one window using their absolute midpoint."""
    kept = []
    for source in segments:
        item = dict(source)
        item['start'] += read_start
        item['end'] += read_start
        if core_start <= (item['start'] + item['end']) / 2 < core_end:
            kept.append(item)
    return kept


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture', type=pathlib.Path)
    parser.add_argument('--model', default='small')
    parser.add_argument('--threads', type=int, default=1)
    parser.add_argument('--chunk-seconds', type=int, default=300)
    parser.add_argument('--vad', action=argparse.BooleanOptionalAction, default=False)
    args = parser.parse_args()
    if not 30 <= args.chunk_seconds <= 300:
        parser.error('chunk-seconds must be between 30 and 300 to bound memory')
    os.environ.setdefault('HF_HUB_DISABLE_XET', '1')
    import av
    import imageio_ffmpeg
    from faster_whisper import WhisperModel
    model = WhisperModel(args.model, device='cpu', compute_type='int8', cpu_threads=args.threads,
                         download_root=str(args.capture.parent / 'models'))
    entries = json.loads((args.capture / 'discovery.json').read_text(encoding='utf-8'))['entries'][:3]
    for entry in entries:
        directory = args.capture / entry['video_id']
        output = directory / 'transcript.asr.json'
        if output.exists():
            print(f"Already captured: {entry['video_id']}", flush=True)
            continue
        audio = directory / 'media.f139.m4a'
        audio_sha = digest(audio)
        with av.open(str(audio)) as container:
            if container.duration is None:
                raise ValueError('Cannot determine full audio duration')
            duration = container.duration / av.time_base
        started = time.monotonic()
        settings = {'beam_size': 5, 'vad_filter': args.vad, 'condition_on_previous_text': False,
                    'chunk_seconds': args.chunk_seconds, 'overlap_seconds': 5, 'model': args.model}
        chunks_dir = directory / 'asr-chunks'
        chunks_dir.mkdir(exist_ok=True)
        captured = []
        with (directory / 'transcript.asr.partial.jsonl').open('w', encoding='utf-8') as stream:
            for index, (core_start, core_end, read_start, read_end) in enumerate(windows(duration, args.chunk_seconds)):
                checkpoint = chunks_dir / f'chunk-{index:04}.json'
                if checkpoint.exists():
                    chunk = json.loads(checkpoint.read_text(encoding='utf-8'))
                    if chunk['audio_sha256'] != audio_sha or chunk['settings'] != settings:
                        raise ValueError('ASR checkpoint source/settings mismatch')
                    items = chunk['segments']
                else:
                    wave = chunks_dir / f'chunk-{index:04}.wav'
                    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
                        '-y', '-ss', str(read_start), '-i', str(audio), '-t', str(read_end-read_start),
                        '-ac', '1', '-ar', '16000', str(wave)], check=True, capture_output=True, timeout=120)
                    try:
                        segments, info = model.transcribe(str(wave), language='zh', beam_size=5,
                            vad_filter=args.vad, condition_on_previous_text=False)
                        items = [{k: getattr(segment, k) for k in
                            ['start', 'end', 'text', 'avg_logprob', 'no_speech_prob']} for segment in segments]
                        items = globalize(items, core_start, core_end, read_start)
                        del segments, info
                        gc.collect()
                        save(checkpoint, {'audio_sha256': audio_sha, 'settings': settings,
                             'core_start': core_start, 'core_end': core_end, 'segments': items})
                    finally:
                        wave.unlink(missing_ok=True)
                for item in items:
                    item = {**item, 'id': len(captured)}
                    captured.append(item)
                    stream.write(json.dumps(item, ensure_ascii=False) + '\n')
                stream.flush()
                print(f"{entry['video_id']}: chunk {index+1}, {core_end:.0f}/{duration:.0f}s saved", flush=True)
                del items
                gc.collect()
        payload = {'source': 'local_automatic_speech_recognition_not_creator_captions',
            'model': args.model, 'engine': 'faster-whisper', 'compute_type': 'int8', 'language': 'zh',
            'audio_duration_seconds': duration, 'audio_sha256': audio_sha,
            'completed_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
            'elapsed_seconds': round(time.monotonic()-started, 2), 'settings': settings,
            'segments': captured}
        save(output, payload)
        (directory / 'transcript.asr.txt').write_text('\n'.join(
            f"[{int(s['start'])//60:02}:{int(s['start'])%60:02}] {s['text']}" for s in captured), encoding='utf-8')
        print(f"Completed {entry['video_id']}: {len(captured)} segments", flush=True)


if __name__ == '__main__':
    main()
