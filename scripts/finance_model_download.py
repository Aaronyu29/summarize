"""Resume a public, pinned Whisper model download with checked byte ranges."""
import argparse
import concurrent.futures
import hashlib
import pathlib
import time
import requests

MODEL_SIZE = 483546902
MODEL_SHA256 = "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671"
URL = "https://huggingface.co/Systran/faster-whisper-small/resolve/536b0662742c02347bc0e980a01041f333bce120/model.bin"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=pathlib.Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    parts = args.directory / "download-parts"
    parts.mkdir(exist_ok=True)
    chunk_size = 8 * 1024 * 1024

    def fetch(start):
        end = min(start + chunk_size, MODEL_SIZE) - 1
        path = parts / f"{start:010}.bin"
        if path.exists() and path.stat().st_size == end - start + 1:
            return path
        for attempt in range(3):
            try:
                response = requests.get(URL, headers={"Range": f"bytes={start}-{end}"}, timeout=(15, 35))
                response.raise_for_status()
                if response.status_code != 206 or response.headers.get("Content-Range") != f"bytes {start}-{end}/{MODEL_SIZE}":
                    raise ValueError("Model server returned an unexpected byte range")
                if len(response.content) != end - start + 1:
                    raise ValueError("Truncated model range")
                path.write_bytes(response.content)
                return path
            except (requests.RequestException, ValueError):
                if attempt == 2:
                    raise
                time.sleep(1)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(fetch, start) for start in range(0, MODEL_SIZE, chunk_size)]
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            future.result()
            print(f"Model ranges: {index}/{len(futures)}", flush=True)
    digest = hashlib.sha256()
    output = args.directory / "model.checked.part"
    with output.open("wb") as stream:
        for path in sorted(parts.glob("*.bin")):
            data = path.read_bytes()
            digest.update(data)
            stream.write(data)
    if output.stat().st_size != MODEL_SIZE or digest.hexdigest() != MODEL_SHA256:
        raise ValueError("Model size or SHA256 mismatch; model will not be activated")
    output.replace(args.directory / "model.bin")
    print("Model verified by size and SHA256", flush=True)


if __name__ == "__main__":
    main()
