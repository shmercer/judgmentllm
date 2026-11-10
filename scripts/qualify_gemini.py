"""Repository-only offline proposal; there is deliberately no execute command."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from judgmentllm.gemini_qualification import main  # noqa: E402

if __name__ == "__main__":
    main()
