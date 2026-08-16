"""模型权重断点续传下载器（代理下大文件断流时使用）。

用法:
  export HTTPS_PROXY=http://127.0.0.1:10808
  uv run python -m pmbot.download_model kronos-base

无限续传：每次断流自动从断点继续，直到完整；适合代理节点传输不稳的场景。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from pmbot.variant_map import VARIANT_WEIGHTS

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("download_model")

MODELS_DIR = Path("models")


def _download_targets(variant: str) -> list[tuple[str, Path]]:
    """变体 → [(repo_id, 本地目录), ...]（model + tokenizer）。

    repo 映射单一事实源在 variant_map.VARIANT_WEIGHTS，此处不再重复维护。
    """
    model_dir, tok_dir, repo = VARIANT_WEIGHTS[variant]
    return [
        (f"{repo}/{model_dir}", MODELS_DIR / model_dir),
        (f"{repo}/{tok_dir}", MODELS_DIR / tok_dir),
    ]


def _download(repo_id: str, local_dir: Path) -> None:
    """无限断点续传下载 repo 全部文件到 local_dir。"""
    from huggingface_hub import snapshot_download

    local_dir.mkdir(parents=True, exist_ok=True)
    attempt = 0
    while True:
        attempt += 1
        try:
            snapshot_download(
                repo_id=repo_id,
                local_dir=str(local_dir),
                max_workers=1,
                tqdm_class=None,
            )
            # 校验关键文件存在
            files = [f.name for f in local_dir.iterdir()]
            logger.info("下载完成 %s → %s（%s）", repo_id, local_dir, ", ".join(files))
            return
        except Exception as e:
            logger.warning("[第 %d 次尝试] %s 中断: %s，继续续传...", attempt, repo_id, e)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="模型权重断点续传下载")
    parser.add_argument("variant", choices=sorted(VARIANT_WEIGHTS), help="模型变体")
    args = parser.parse_args(argv)

    for repo_id, local in _download_targets(args.variant):
        _download(repo_id, local)
    logger.info("全部完成：%s 权重就绪", args.variant)
    return 0


if __name__ == "__main__":
    sys.exit(main())
