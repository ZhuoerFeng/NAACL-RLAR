#!/usr/bin/env python3
"""Resumable, checked HTTP ranges for the pinned official JSONL download."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time

import requests
from download_nemotron import ROOT, REPO, REVISION

CHUNK = 32 * 1024 * 1024
LOCAL = threading.local()


def main():
    entries = [x for x in json.loads((ROOT / "upstream_tree.json").read_text()) if x["path"].endswith(".jsonl")]
    lock, contexts = threading.Lock(), {}
    for entry in entries:
        target = ROOT / entry["path"]
        state_path = target.with_suffix(".ranges.json")
        temp = target.with_suffix(".ranges.partial")
        if target.exists():
            continue
        if state_path.exists():
            state = json.loads(state_path.read_text())
            assert state["revision"] == REVISION and state["chunk_bytes"] == CHUNK
        else:
            previous = target.with_suffix(".jsonl.partial")
            n = previous.stat().st_size // CHUNK if previous.exists() else 0
            if previous.exists():
                previous.rename(temp)
            state = {"revision": REVISION, "chunk_bytes": CHUNK, "complete": list(range(n))}
            state_path.write_text(json.dumps(state))
        fd = os.open(temp, os.O_CREAT | os.O_RDWR, 0o644)
        os.ftruncate(fd, entry["size"])
        contexts[entry["path"]] = {"entry": entry, "fd": fd, "state": state, "state_path": state_path, "temp": temp}

    def fetch(context, idx):
        entry, fd = context["entry"], context["fd"]
        chunk_start, end = idx * CHUNK, min((idx + 1) * CHUNK, entry["size"]) - 1
        if not hasattr(LOCAL, "session"):
            LOCAL.session = requests.Session()
        for attempt in range(6):
            try:
                # JSONL cannot contain literal NUL bytes. In the sparse staging
                # file the first NUL therefore marks the unwritten prefix boundary.
                # Resume that boundary even after a process restart or read timeout.
                # Completed output still requires the official whole-file SHA-256.
                block = os.pread(fd, end - chunk_start + 1, chunk_start)
                unwritten = block.find(b"\x00")
                start = end + 1 if unwritten < 0 else chunk_start + unwritten
                url = f"https://huggingface.co/datasets/{REPO}/resolve/{REVISION}/{entry['path']}?download=true&chunk={idx}&offset={start}"
                if start <= end:
                    transfer(context, start, end, url)
                with lock:
                    os.fsync(fd)
                    context["state"]["complete"].append(idx)
                    temp_state = context["state_path"].with_suffix(".tmp")
                    temp_state.write_text(json.dumps(context["state"]))
                    temp_state.replace(context["state_path"])
                return
            except Exception as exc:
                print(f"Retry {attempt+1}: {entry['path']} chunk {idx}: {type(exc).__name__}", flush=True)
                if attempt == 5:
                    raise RuntimeError(f"Range {idx} failed for {entry['path']}: {type(exc).__name__}") from None
                time.sleep(min(2 ** attempt, 16))

    def transfer(context, start, end, url):
        entry, fd = context["entry"], context["fd"]
        with LOCAL.session.get(url, headers={"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}, stream=True, timeout=(30, 45)) as response:
            if response.status_code != 206:
                raise RuntimeError(f"HTTP {response.status_code}, expected 206")
            if response.headers.get("Content-Range") != f"bytes {start}-{end}/{entry['size']}":
                raise RuntimeError("Unexpected Content-Range")
            position = start
            for block in response.iter_content(1024 * 1024):
                if position + len(block) > end + 1:
                    raise RuntimeError("Range exceeded requested bytes")
                view = memoryview(block)
                while view:
                    count = os.pwrite(fd, view, position)
                    position += count
                    view = view[count:]
            if position != end + 1:
                raise RuntimeError("Incomplete HTTP range")

    jobs = []
    for context in contexts.values():
        done = set(context["state"]["complete"])
        jobs.extend((context, i) for i in range((context["entry"]["size"] + CHUNK - 1) // CHUNK) if i not in done)
    # Interleave files so neither section waits for all chunks of the other.
    jobs.sort(key=lambda pair: pair[1])
    last = 0
    with ThreadPoolExecutor(max_workers=int(os.environ.get("NEMOTRON_DOWNLOAD_WORKERS", "32"))) as pool:
        futures = [pool.submit(fetch, *job) for job in jobs]
        for future in as_completed(futures):
            future.result()
            now = time.monotonic()
            if now - last >= 20:
                with lock:
                    progress = [(c["entry"]["path"], sum(min(CHUNK, c["entry"]["size"] - idx * CHUNK) for idx in c["state"]["complete"]), c["entry"]["size"]) for c in contexts.values()]
                print(" | ".join(f"{name}: {n / 1e9:.2f}/{total / 1e9:.2f} GB" for name, n, total in progress), flush=True)
                last = now
    files = []
    for entry in entries:
        target = ROOT / entry["path"]
        context = contexts.get(entry["path"])
        if context:
            os.fsync(context["fd"]); os.close(context["fd"])
        check = context["temp"] if context else target
        with check.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        assert checksum == entry["lfs"]["oid"], f"LFS checksum mismatch: {entry['path']}"
        if context:
            check.rename(target)
        files.append({"path": entry["path"], "bytes": target.stat().st_size, "sha256": checksum, "verified_against_hf_lfs": True})
        print(f"Verified {target.name}", flush=True)
    (ROOT / "download_manifest.json").write_text(json.dumps({"repo": REPO, "revision": REVISION, "downloaded_utc": datetime.now(timezone.utc).isoformat(), "files": files}, indent=2) + "\n")
    print("Download complete and both LFS checksums verified", flush=True)


if __name__ == "__main__":
    main()
