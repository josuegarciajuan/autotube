"""Test channel config — inherits from canal2 (Sincronías) for algorithm testing.

This channel is used exclusively by --fast-test mode to validate pipeline changes
without affecting production channels. Videos are NOT uploaded to YouTube.
"""

# Inherit everything from canal2 config (the most complete config)
from config.canal2_config import *

# Override channel identity
CANAL_NAME = "test"
CANAL_DISPLAY_NAME = "Pruebas de algoritmo"
CANAL_TAGLINE = "Canal de pruebas internas — no publicar"

TEST_MODE = False  # not used; test_video.py monkey-patches this
