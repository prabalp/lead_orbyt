"""Manual smoke test for the full pipeline, run directly (no MCP transport).

    conda activate orbyt
    python test_run.py
"""

import asyncio

from leadorbyt.server import find_leads


async def main():
    path = await find_leads("coffee shops", "Austin, TX", 5)
    print(f"\nCSV written to: {path}")
    with open(path) as f:
        print(f.read())


if __name__ == "__main__":
    asyncio.run(main())
