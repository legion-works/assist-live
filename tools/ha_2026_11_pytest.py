from importlib.metadata import version

import homeassistant  # noqa: F401

# HA must install its `httpx2` alias before test plugin `0.13.375` imports `respx`.
# Use `pytest -p` to load this module after pytest starts assertion rewriting.
if (
    version("homeassistant") == "2026.11.2"
    and version("pytest-homeassistant-custom-component") == "0.13.375"
):
    from homeassistant.components import logger
    from homeassistant.components.logger.helpers import _clear_logger_overwrites

    # HA exports this helper from `logger.helpers`, not from `logger`.
    # Remove this adapter when a test plugin release uses the new import path.
    vars(logger)["_clear_logger_overwrites"] = _clear_logger_overwrites
