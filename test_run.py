"""Manual smoke test for discovery, run directly (no MCP transport).

    conda activate orbyt
    python test_run.py
"""

import asyncio

from leadorbyt import auth
from leadorbyt.server import find_leads


async def main():
    token = auth.current_user_id.set("manual-smoke")
    try:
        result = await find_leads("coffee shops", "Austin, TX", 5)
    finally:
        auth.current_user_id.reset(token)
    path = result["result_path"]
    print(f"\nCSV written to: {path}")
    with open(path) as f:
        print(f.read())


if __name__ == "__main__":
    asyncio.run(main())
