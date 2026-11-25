"""Start vfx-transcode-process: python vfx-transcode-process.py [file or folder ...]"""
import sys

from vfx_process.app import main

if __name__ == "__main__":
    sys.exit(main())
