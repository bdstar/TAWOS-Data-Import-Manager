"""Start the TAWOS Data Import Manager web server.

    python run.py
"""

import uvicorn

from app.config import Settings
from app.main import create_app

if __name__ == "__main__":
    settings = Settings.from_env()
    # A single worker process: import jobs run on background threads owned by this process.
    uvicorn.run(create_app(settings), host=settings.app_host, port=settings.app_port, workers=1,
                log_level="info")
