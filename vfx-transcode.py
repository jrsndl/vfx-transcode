"""Entry script kept for existing .bat files and the PyInstaller spec.

The code lives in the vfx_transcode package; `python -m vfx_transcode` works too.
"""

import sys

from vfx_transcode.cli import main

if __name__ == "__main__":
    sys.exit(main())
