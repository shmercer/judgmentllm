"""Repository harness; live execution requires separate plan-specific consent."""

import sys
from pathlib import Path

# This repository-only harness must execute the reviewed checkout, even when the
# developer environment has another checkout installed in editable mode.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from judgmentllm.qualification import main  # noqa: E402

if __name__ == "__main__":
    main()
