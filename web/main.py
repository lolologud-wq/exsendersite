"""Entry point for the site (control plane)."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

load_dotenv(HERE / ".env")

logging.basicConfig(
    level=os.getenv("SITE_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from app import create_app  # noqa: E402

app = create_app()


def main() -> None:
    host = os.getenv("SITE_HOST", "0.0.0.0")
    port = int(os.getenv("SITE_PORT", "3000"))
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=False,
        log_level=os.getenv("SITE_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
