"""Guarda tokens.json + whoop.db criptografados (AES-GCM) em state.enc.

O GitHub Actions usa isso para manter o login do WHOOP e o historico entre execucoes,
sem nada legivel no repositorio. A chave fica no secret STATE_KEY.

Uso:
    python state_crypto.py pack      # tokens.json + whoop.db -> state.enc
    python state_crypto.py unpack    # state.enc -> tokens.json + whoop.db
    python state_crypto.py pull      # baixa state.enc do branch "state" e abre (uso local)
    python state_crypto.py newkey    # gera uma chave nova
"""
import base64
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BASE_DIR = Path(__file__).parent
STATE_DIR = Path(os.environ.get("WHOOP_STATE_DIR", BASE_DIR))
STATE_FILE = BASE_DIR / "state.enc"
FILES = ("tokens.json", "whoop.db")


def get_key():
    key = os.environ.get("STATE_KEY")
    if not key and (BASE_DIR / "config.json").exists():
        key = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8")).get("state_key")
    if not key:
        sys.exit("STATE_KEY nao definido.")
    return base64.b64decode(key)


def pack():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in FILES:
            if (STATE_DIR / name).exists():
                z.write(STATE_DIR / name, name)
    nonce = os.urandom(12)
    STATE_FILE.write_bytes(nonce + AESGCM(get_key()).encrypt(nonce, buf.getvalue(), None))
    print(f"Estado salvo em {STATE_FILE.name}")


def unpack():
    if not STATE_FILE.exists():
        print("Nenhum estado salvo ainda.")
        return
    raw = STATE_FILE.read_bytes()
    data = AESGCM(get_key()).decrypt(raw[:12], raw[12:], None)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(STATE_DIR)
    print("Estado restaurado.")


def pull():
    subprocess.run(["git", "fetch", "-q", "origin", "state"], cwd=BASE_DIR, check=True)
    blob = subprocess.run(["git", "show", "origin/state:state.enc"], cwd=BASE_DIR, check=True, capture_output=True).stdout
    STATE_FILE.write_bytes(blob)
    unpack()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "newkey":
        print(base64.b64encode(os.urandom(32)).decode())
    elif cmd in ("pack", "unpack", "pull"):
        globals()[cmd]()
    else:
        sys.exit(__doc__)
