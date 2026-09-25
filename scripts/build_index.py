"""Build the plot-embedding cache used by content search.

    python scripts/build_index.py                           # local bge-small (~29 min on CPU)
    python scripts/build_index.py --backend openai-3-small  # OpenAI (~2 min, ~$0.08)
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from movie_agent.content import build_embeddings  # noqa: E402
from movie_agent.data import MovieData  # noqa: E402
from movie_agent.embedders import BACKENDS  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=BACKENDS, default=None)
    args = ap.parse_args()
    t0 = time.time()
    build_embeddings(MovieData.load(), args.backend)
    print(f"done in {time.time() - t0:.0f}s")
