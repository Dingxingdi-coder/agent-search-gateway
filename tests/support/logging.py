import io
import logging
from collections.abc import Iterable

from agent_search_gateway.observability import KeyValueFormatter, SecretRedactor, SecretValue


def structured_test_logger(
    name: str,
    *,
    secrets: Iterable[SecretValue] = (),
) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(KeyValueFormatter(SecretRedactor(secrets)))
    logger.addHandler(handler)
    return logger, stream
