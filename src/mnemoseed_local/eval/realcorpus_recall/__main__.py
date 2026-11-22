"""``python -m mnemoseed_local.eval.realcorpus_recall`` entry point."""

from __future__ import annotations

import sys

from mnemoseed_local.eval.realcorpus_recall.cli import main

if __name__ == "__main__":
    sys.exit(main())
