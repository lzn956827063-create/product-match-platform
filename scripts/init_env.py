"""Create deployment configuration and secret files without replacing existing values."""
import base64
import os
import secrets
from pathlib import Path


def create_secret(path, value):
    if path.exists():
        return False
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(value)
    return True


def main():
    secret_dir = Path(os.getenv("SECRETS_DIR", "var/secrets"))
    secret_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    secret_dir.chmod(0o700)
    generated = []
    values = {
        "db_bootstrap_password": secrets.token_hex(24),
        "db_owner_password": secrets.token_hex(24),
        "db_app_password": secrets.token_hex(24),
        "db_readonly_password": secrets.token_hex(24),
        "db_backup_password": secrets.token_hex(24),
        "jwt_secret": secrets.token_hex(48),
        "s3_root_secret": secrets.token_hex(32),
        "s3_app_secret": secrets.token_hex(32),
        "metrics_token": secrets.token_hex(32),
        "integration_master_key": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "grafana_password": secrets.token_hex(24),
    }
    for name, value in values.items():
        path = secret_dir / name
        if create_secret(path, value):
            generated.append(str(path))
        path.chmod(0o600)

    env_path = Path(".env")
    if not env_path.exists():
        env_path.write_text(
            "SECRETS_DIR=./var/secrets\n"
            "S3_ROOT_ACCESS_KEY=productmatch-root\n"
            "S3_ACCESS_KEY=productmatch-app\n"
            "WEB_PORT=8080\n"
            "ALLOWED_ORIGINS=http://localhost:8080,http://127.0.0.1:8080\n"
            "COOKIE_SECURE=false\n"
            "WEBHOOK_ALLOWED_HOSTS=\n"
        )
        env_path.chmod(0o600)
        generated.append(str(env_path))
    print(f"Created {len(generated)} configuration files; secret values were not printed.")


if __name__ == "__main__":
    main()
