"""Compatibility entry point; the modular implementation starts in main.py."""

import asyncio

from main import main


if __name__ == "__main__":
    asyncio.run(main())
