import os

DEFAULT_HOST = os.environ.get("RESOLUME_HOST", "localhost")
DEFAULT_PORT = int(os.environ.get("RESOLUME_PORT", "8080"))
DEFAULT_TIMEOUT = float(os.environ.get("RESOLUME_TIMEOUT", "10.0"))


def get_ws_uri(host=DEFAULT_HOST, port=DEFAULT_PORT) -> str:
    return f"ws://{host}:{port}/api/v1"
