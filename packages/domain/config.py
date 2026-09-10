import os
from pathlib import Path
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "var"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
MODEL_DIR = Path(os.getenv("MODEL_DIR", ROOT / "models"))


def secret_value(name, default=None):
    """Read a secret from NAME_FILE first, then NAME, without logging its value."""
    filename = os.getenv(name + "_FILE")
    if filename:
        return Path(filename).read_text().strip()
    return os.getenv(name, default)


def database_url():
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        return explicit
    password = secret_value("DB_PASSWORD")
    if password is not None:
        user = quote_plus(os.getenv("DB_USER", "productmatch_app"))
        password = quote_plus(password)
        host = os.getenv("DB_HOST", "postgres")
        port = int(os.getenv("DB_PORT", "5432"))
        name = quote_plus(os.getenv("DB_NAME", "productmatch"))
        return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"
    return f"sqlite:///{DATA_DIR / 'app.db'}"


DATABASE_URL = database_url()
QUEUE_MODE = os.getenv("QUEUE_MODE", "database")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173,http://localhost:18765,http://127.0.0.1:18765").split(",")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
secret_path = DATA_DIR / "session.key"
configured_jwt_secret = secret_value("JWT_SECRET")
if configured_jwt_secret:
    JWT_SECRET = configured_jwt_secret
else:
    import secrets
    try:
        fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(48))
    except FileExistsError:
        pass
    JWT_SECRET = secret_path.read_text()
