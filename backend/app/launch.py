import uvicorn

from .config import load_settings
from .main import app


def main():
    settings = load_settings()
    config = uvicorn.Config(
        app, host=settings.host, port=settings.port, workers=1, access_log=False, timeout_graceful_shutdown=5
    )
    server = uvicorn.Server(config)

    def shutdown():
        app.state.shutdown_requested = True
        server.should_exit = True

    app.state.request_shutdown = shutdown
    server.run()


if __name__ == "__main__":
    main()
