"""Create deployment secrets without overwriting an existing configuration."""
import secrets
from pathlib import Path

path=Path('.env')
if path.exists():
    raise SystemExit('.env already exists; nothing changed.')
with path.open('x') as f:
    f.write(f'POSTGRES_PASSWORD={secrets.token_hex(24)}\nJWT_SECRET={secrets.token_hex(48)}\nS3_ACCESS_KEY=productmatch\nS3_SECRET_KEY={secrets.token_hex(32)}\nWEB_PORT=8080\nALLOWED_ORIGINS=http://localhost:8080,http://127.0.0.1:8080\nCOOKIE_SECURE=false\n')
path.chmod(0o600)
print('Created .env. Secret values are not printed.')
