import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "var"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
MODEL_DIR = Path(os.getenv("MODEL_DIR", ROOT / "models"))
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'app.db'}")
QUEUE_MODE = os.getenv("QUEUE_MODE", "database")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173,http://localhost:18765,http://127.0.0.1:18765").split(",")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
secret_path = DATA_DIR / "session.key"
if os.getenv("JWT_SECRET"):
    JWT_SECRET = os.environ["JWT_SECRET"]
else:
    import secrets
    try:
        fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(48))
    except FileExistsError:
        pass
    JWT_SECRET = secret_path.read_text()
