from __future__ import annotations

import logging
import sys


def test_pivota_logger_uses_stdout_for_non_error_logs() -> None:
    import utils.logger as logger_module

    handlers = [
        handler
        for handler in logger_module.logger.handlers
        if getattr(handler, "_pivota_stdout_handler", False)
    ]

    assert handlers
    assert all(isinstance(handler, logging.StreamHandler) for handler in handlers)
    assert all(handler.stream is sys.stdout for handler in handlers)
    assert logger_module.logger.propagate is False
