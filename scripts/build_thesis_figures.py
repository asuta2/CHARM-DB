"""Build the selective final thesis figure set without connecting to PostgreSQL."""

import argparse
from pathlib import Path

from charmdb.reporting.figures import render

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("C:/CHARMDB-ARTIFACTS/v2"))
    parser.add_argument(
        "--output", type=Path, required=True, help="New directory outside the evidence tree"
    )
    args = parser.parse_args()
    result = render(args.root, args.output)
    print(f"Wrote six thesis figures and five refreshed primary figures to {args.output}")
    print(f"Authenticated {len(result['verified_source_files'])} source files")
