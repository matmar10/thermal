"""Thermal: compose print jobs from a handful of primitives and send them to an
ESC/POS printer, from the command line or over HTTP.

The pipeline is the same for both front ends:

  JSON job/template --job--> job --layout--> layout --escpos--> bytes --transport--> printer
                                                   \\--preview--> text

Layout is the single place where paper geometry is applied, so the bytes and
the preview cannot disagree about where a line wraps.

Standard library only (Python 3.9+), so it runs anywhere Python does.
"""

__version__ = "0.3.0"
