"""One place that decides what a log line looks like.

Loggers are named explicitly (`get("liqo")`) rather than via `__name__`. With
`__name__` the column would read `autoscaler.providers.ovh.provider`, which is
mostly package structure the reader already knows; the short name is the part
that says which failure domain spoke.
"""
import logging
import sys

FORMAT = "[%(asctime)s] %(name)-12s %(levelname)-7s %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup(level="INFO"):
    """Configure the root logger. Call once, from __main__, before anything logs.

    StreamHandler flushes on every record, so lines reach `kubectl logs` as they
    happen — the property the old `print(..., flush=True)` was buying.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(FORMAT, datefmt=DATEFMT))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    # The kubernetes client logs every request at DEBUG and urllib3 warns on
    # each retry. Useful when chasing an API problem, deafening otherwise, and
    # they would drown the one line per pass that says what the operator decided.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("kubernetes").setLevel(logging.WARNING)


def get(name):
    return logging.getLogger(name)
