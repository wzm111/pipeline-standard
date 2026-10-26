#!/usr/bin/env python3
"""Installer: initialize local preferences once, never overwrite an upgrade."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from model_config import DEFAULT_MODELS, read_document


def initialize(target, seed=DEFAULT_MODELS):
    target = Path(target)
    if target.exists() or target.is_symlink():
        read_document(target)  # Corrupt/unknown versions are preserved but reported.
        return False
    document, revision = read_document(seed)
    if revision is None:
        raise ValueError("skill default template missing")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".pipeline-models-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write((json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Atomic no-clobber creation, unlike replace: upgrade cannot erase choices.
            os.link(temporary, target)
        except FileExistsError:
            read_document(target)
            return False
        read_document(target)
        return True
    finally:
        if temporary is not None:
            temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    try:
        changed = initialize(args.target)
    except (OSError, ValueError) as error:
        parser.exit(1, f"本机模型配置初始化失败，原配置未覆盖：{error}\n")
    print(f'{"已初始化" if changed else "已保留"}本机模型配置: {args.target}')


if __name__ == "__main__":
    main()
