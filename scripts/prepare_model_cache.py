#!/usr/bin/env python3
"""
HuggingFace Embedding Model Cache Preparation and Offline Validation Script (Phase 10.1.20).

Purpose:
Prepares and validates the exact HuggingFace cache structure required by the
production backend before launching containers with TRANSFORMERS_OFFLINE=1.

Usage:
1. Online Machine (with internet access):
   python scripts/prepare_model_cache.py download --target-dir ./hf_cache

2. Offline Host / Container (offline mode verification):
   python scripts/prepare_model_cache.py validate --cache-dir ./hf_cache

3. Verification of missing cache error behavior (negative test):
   python scripts/prepare_model_cache.py test-offline-failure --empty-dir ./empty_temp
"""
import argparse
import os
from pathlib import Path
import sys

MODEL_ID = "BAAI/bge-small-en-v1.5"
PINNED_REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"


def download_model(target_dir: Path, revision: str = PINNED_REVISION) -> None:
    """
    Downloads model weights and tokenizer to the designated target directory.
    Run ONLY on a machine with internet access.

    Directory Contract:
    huggingface_hub places downloaded repositories inside a `hub/` subdirectory
    under `target_dir`:
      target_dir/
      └── hub/
          └── models--BAAI--bge-small-en-v1.5/
              ├── snapshots/
              │   └── 5c38ec7c405ec4b44b94cc5a9bb96e735b38267a/
              └── refs/
                  └── main

    When staging into Docker or deploying to production, the ENTIRE contents of
    `target_dir` (including the `hub/` folder) must be placed at the root of the
    persistent volume mounted to `/home/sebi/.cache/huggingface`.
    """
    print(f"[*] Preparing model download for '{MODEL_ID}' (revision: {revision})...")
    print(f"[*] Target directory root: {target_dir.resolve()}")
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("[!] Error: huggingface_hub is required for download. Run: pip install huggingface_hub", file=sys.stderr)
        sys.exit(1)

    # Set cache dir to target
    snapshot_download(
        repo_id=MODEL_ID,
        revision=revision,
        cache_dir=str(target_dir),
        local_files_only=False,
    )
    print(f"[+] Successfully downloaded and cached '{MODEL_ID}' into {target_dir}/hub")
    print(f"[+] Note: When copying to named volumes, transfer the root containing 'hub/' directly.")



def validate_offline_cache(cache_dir: Path) -> bool:
    """
    Validates that SentenceTransformer can load the model strictly offline
    from cache_dir with TRANSFORMERS_OFFLINE=1 and HF_HUB_OFFLINE=1.
    """
    print(f"[*] Validating offline model loading from: {cache_dir.resolve()}")
    if not cache_dir.exists():
        print(f"[!] Error: Cache directory does not exist: {cache_dir}", file=sys.stderr)
        return False

    # Enforce strict offline environment
    os.environ["HF_HOME"] = str(cache_dir.resolve())
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"

    try:
        from sentence_transformers import SentenceTransformer
        # Attempt loading
        model = SentenceTransformer(MODEL_ID, device="cpu")
        test_embedding = model.encode("SEBI regulatory circular compliance", normalize_embeddings=True)
        print(f"[+] Offline model load SUCCESSFUL. Embedding shape: {test_embedding.shape}")
        return True
    except Exception as e:
        print(f"[!] Offline model load FAILED: {e}", file=sys.stderr)
        return False


def test_offline_failure(empty_dir: Path) -> bool:
    """
    Negative verification: Confirms that attempting to load a model with an empty
    cache under offline mode fails explicitly rather than attempting an online download.
    """
    print(f"[*] Running negative offline test against empty directory: {empty_dir.resolve()}")
    empty_dir.mkdir(parents=True, exist_ok=True)

    os.environ["HF_HOME"] = str(empty_dir.resolve())
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"

    try:
        from sentence_transformers import SentenceTransformer
        _ = SentenceTransformer(MODEL_ID, device="cpu")
        print("[!] ERROR: Model loaded unexpectedly from empty directory!", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[+] Correct failure caught as expected under offline mode: {type(e).__name__}: {str(e)[:120]}...")
        return True


def main():
    parser = argparse.ArgumentParser(description="HuggingFace Model Cache Tool for SEBI Assistant")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Download command
    p_dl = subparsers.add_parser("download", help="Download model weights on an online host")
    p_dl.add_argument("--target-dir", type=Path, required=True, help="Destination directory for cache")
    p_dl.add_argument("--revision", type=str, default=PINNED_REVISION, help="Pinned Git commit SHA")

    # Validate command
    p_val = subparsers.add_parser("validate", help="Validate offline loading from existing cache")
    p_val.add_argument("--cache-dir", type=Path, required=True, help="Path to HF_HOME cache root")

    # Negative test command
    p_neg = subparsers.add_parser("test-offline-failure", help="Verify negative error behavior")
    p_neg.add_argument("--empty-dir", type=Path, required=True, help="Empty test directory")

    args = parser.parse_args()

    if args.command == "download":
        download_model(args.target_dir, args.revision)
    elif args.command == "validate":
        success = validate_offline_cache(args.cache_dir)
        sys.exit(0 if success else 1)
    elif args.command == "test-offline-failure":
        success = test_offline_failure(args.empty_dir)
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
