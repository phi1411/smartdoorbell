from __future__ import annotations

import logging
import os

from smart_doorbell.app_factory import create_app
from smart_doorbell.config import AppConfig


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


if __name__ == "__main__":
    configure_logging()
    config = AppConfig.from_env()
    application = create_app(config)
    print(f"Smart Doorbell: http://{config.host}:{config.port}")
    application.run(host=config.host, port=config.port, threaded=True, use_reloader=False)

