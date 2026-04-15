import logging
import sys

logger = logging.getLogger("pivota")
logger.setLevel(logging.INFO)
logger.propagate = False

if not any(getattr(handler, "_pivota_stdout_handler", False) for handler in logger.handlers):
    ch = logging.StreamHandler(sys.stdout)
    ch._pivota_stdout_handler = True
    formatter = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s")
    ch.setFormatter(formatter)
    logger.addHandler(ch)
