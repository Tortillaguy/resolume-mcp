import os

DEFAULT_HOST = os.environ.get("RESOLUME_HOST", "localhost")
DEFAULT_PORT = int(os.environ.get("RESOLUME_PORT", "8080"))
DEFAULT_TIMEOUT = float(os.environ.get("RESOLUME_TIMEOUT", "120.0"))
BEHAVIORS_PATH = os.environ.get(
    "RESOLUME_BEHAVIORS_PATH",
    os.path.join(os.path.expanduser("~"), ".resolume-mcp", "behaviors.json"),
)
SNAPSHOTS_DIR = os.environ.get(
    "RESOLUME_SNAPSHOTS_DIR",
    os.path.join(os.path.expanduser("~"), ".resolume-mcp", "snapshots"),
)


def get_ws_uri(host=DEFAULT_HOST, port=DEFAULT_PORT) -> str:
    return f"ws://{host}:{port}/api/v1"
