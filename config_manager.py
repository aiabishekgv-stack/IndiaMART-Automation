import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv, set_key


ROOT = Path(__file__).resolve().parent
LOCAL_ROOT = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "NUNES_INDIAMART_AUTOMATION"
CONFIG_DIR = LOCAL_ROOT / "config"
LOCAL_ENV = CONFIG_DIR / ".env"
PROJECT_ENV = ROOT / ".env"  # legacy compatibility only; V2.4 never writes secrets here.
EXAMPLE_ENV = ROOT / ".env.example"


def ensure_local_config():
    """Use Vercel environment variables or Windows local configuration."""
    if os.getenv("VERCEL"):
        return None

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not LOCAL_ENV.exists():
        if EXAMPLE_ENV.exists():
            LOCAL_ENV.write_text(EXAMPLE_ENV.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            LOCAL_ENV.touch()
    return LOCAL_ENV


def load_configuration():
    """
    Priority (highest to lowest):
      1. Explicit process environment, e.g. START_LAN.bat APP_HOST=0.0.0.0
      2. Private per-PC LocalAppData .env
      3. Legacy project .env (read-only compatibility)
      4. Code defaults

    New secrets/settings are always written to LOCAL_ENV.
    """
    explicit_keys = set(os.environ.keys())

    if PROJECT_ENV.exists():
        load_dotenv(PROJECT_ENV, override=False)

    config_path = ensure_local_config()
    if config_path is None:
        return None
    local_values = dotenv_values(config_path)
    for key, value in local_values.items():
        if key and value is not None and key not in explicit_keys:
            os.environ[key] = str(value)

    return LOCAL_ENV


def save_local_setting(key, value):
    if os.getenv("VERCEL"):
        raise RuntimeError("Use Vercel environment variables for cloud settings.")
    ensure_local_config()
    set_key(str(LOCAL_ENV), str(key), str(value), quote_mode="never")
    os.environ[str(key)] = str(value)


def local_config_path():
    return ensure_local_config()
