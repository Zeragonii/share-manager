from pathlib import Path
import os


def get_version() -> str:
    """Return the application version from APP_VERSION or the bundled VERSION file."""
    env_version = os.getenv("APP_VERSION", "").strip()
    if env_version:
        return env_version

    candidates = [
        Path(__file__).resolve().parent.parent / "VERSION",
        Path("/app/VERSION"),
    ]
    for path in candidates:
        try:
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError:
            continue
    return "dev"


APP_VERSION = get_version()
