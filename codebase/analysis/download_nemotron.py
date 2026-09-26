#!/usr/bin/env python3
"""Download both official Nemotron v2 JSONL files at a pinned revision."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request

REPO = "nvidia/Nemotron-SFT-Instruction-Following-Chat-v2"
REVISION = "1a9454ed054b8544503ab8d8c0a519d141a44c5b"
ROOT = Path(__file__).resolve().parents[1] / "data/raw/nemotron-instruction-following-chat-v2"


def read_url(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def download(entry):
    target = ROOT / entry["path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    expected_hash, size = entry["lfs"]["oid"], entry["size"]
    partial = target.with_suffix(target.suffix + ".partial")
    if not target.exists():
        url = f"https://huggingface.co/datasets/{REPO}/resolve/{REVISION}/{entry['path']}?download=true"
        os.environ.setdefault("HF_XET_HIGH_PERFORMANCE", "1")
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        from huggingface_hub import hf_hub_download
        hf_hub_download(repo_id=REPO, repo_type="dataset", filename=entry["path"],
                        revision=REVISION, local_dir=ROOT, token=False)
        check = target
    else:
        check = target
    if check.stat().st_size != size:
        raise ValueError(f"Size mismatch for {check}: {check.stat().st_size} != {size}")
    with check.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    if checksum != expected_hash:
        raise ValueError(f"SHA-256 mismatch for {check}")
    if check == partial:
        partial.rename(target)
    elif partial.exists():
        partial.unlink()  # Superseded partial from the initial HTTP download.
    print(f"Verified {target.name}: {size:,} bytes, SHA-256 {checksum}", flush=True)
    return {"path": entry["path"], "bytes": size, "sha256": checksum, "verified_against_hf_lfs": True}


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    metadata = read_url(f"https://huggingface.co/api/datasets/{REPO}/revision/{REVISION}")
    tree_bytes = read_url(f"https://huggingface.co/api/datasets/{REPO}/tree/{REVISION}?recursive=true")
    (ROOT / "upstream_metadata.json").write_bytes(metadata)
    (ROOT / "upstream_tree.json").write_bytes(tree_bytes)
    (ROOT / "README.md").write_bytes(read_url(f"https://huggingface.co/datasets/{REPO}/raw/{REVISION}/README.md"))
    entries = [entry for entry in json.loads(tree_bytes) if entry.get("path", "").endswith(".jsonl")]
    print(f"Downloading {len(entries)} files, {sum(x['size'] for x in entries):,} bytes total", flush=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(download, entry) for entry in entries]
        while not all(job.done() for job in jobs):
            amounts = []
            for entry in entries:
                path = ROOT / entry["path"]
                if not path.exists():
                    path = path.with_suffix(path.suffix + ".partial")
                amount = path.stat().st_size if path.exists() else 0
                amounts.append(f"{path.stem}: {amount / 1e9:.2f}/{entry['size'] / 1e9:.2f} GB")
            print(" | ".join(amounts), flush=True)
            time.sleep(20)
        files = [job.result() for job in jobs]
    manifest = {"repo": REPO, "revision": REVISION, "downloaded_utc": datetime.now(timezone.utc).isoformat(), "files": files}
    (ROOT / "download_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Download and verification complete", flush=True)


if __name__ == "__main__":
    main()
