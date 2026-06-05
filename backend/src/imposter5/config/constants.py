import os

STATIC_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ALLOWED_STATIC_FILES: set[str] = set()

ALLOWED_STATIC_PREFIXES: tuple[str, ...] = (
    "/data/",
    "/docs/",
    "/js/",
    "/assets/",
    "/.codex-outputs/",
)


def is_allowed_static_path(path: str) -> bool:
    if not path:
        return False
    if path == "/":
        return True
    return any(path.startswith(pfx) for pfx in ALLOWED_STATIC_PREFIXES)
