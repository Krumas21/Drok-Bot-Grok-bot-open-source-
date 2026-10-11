"""Entry point: `drok` (or `python -m drok`)."""

import logging
import sys

import uvicorn

from . import config
from .app import App
from .web.app import create_web


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = config.load()
    if not settings.admin_password:
        sys.exit("DROK_ADMIN_PASSWORD is not set. Put it in .env - the web UI controls agents with shell access.")
    if not settings.computer_token:
        logging.warning("COMPUTER_TOKEN is not set: the computer will reject every request.")
    web = create_web(App(settings))
    uvicorn.run(web, host=settings.host, port=settings.port, log_level="warning", proxy_headers=True)


if __name__ == "__main__":
    main()
