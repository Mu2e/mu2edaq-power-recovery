"""``python -m mu2edaq_power_recovery`` -- the same entry point as the console
script, so the self-update re-exec has something stable to restart into."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
