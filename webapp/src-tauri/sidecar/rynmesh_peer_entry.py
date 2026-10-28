"""PyInstaller entry: the stock Ryn node daemon, frozen self-contained.

This is the unmodified rynmesh peer (`rynmesh.peer_http:main`); freezing only
removes the system-Python/rynmesh install requirement. Behavior is identical.
"""
import sys
from multiprocessing import freeze_support

if __name__ == "__main__":
    # Dispatch supervised offline download children before importing the node.
    # PyInstaller requires this on every platform, including macOS spawn.
    freeze_support()
    from rynmesh.peer_http import main

    sys.exit(main())
