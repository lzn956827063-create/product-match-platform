"""Create deployment secrets without overwriting an existing configuration."""
import secrets
from pathlib import Path

secret_dir=Path('var/secrets');secret_dir.mkdir(parents=True,exist_ok=True);secret_dir.chmod(0o700)
monitor=secret_dir/'metrics_token'
if not monitor.exists():
    monitor.write_text(secrets.token_hex(32))
monitor.chmod(0o644)
path=Path('.env')
if path.exists():
    raise SystemExit('.env already exists; nothing changed.')
with path.open('x') as f:
    f.write(f'POSTGRES_PASSWORD={secrets.token_hex(24)}\nJWT_SECRET={secrets.token_hex(48)}\nS3_ACCESS_KEY=productmatch\nS3_SECRET_KEY={secrets.token_hex(32)}\nWEB_PORT=8080\nALLOWED_ORIGINS=http://localhost:8080,http://127.0.0.1:8080\nCOOKIE_SECURE=false\nGRAFANA_PASSWORD={secrets.token_hex(24)}\nWEBHOOK_ALLOWED_HOSTS=\n')
path.chmod(0o600)
print('Created .env. Secret values are not printed.')
