#!/usr/bin/env python3
"""Set the HUD owner login. Reads the password from stdin (never from argv, so it stays out of
`ps` and shell history), stores only an argon2id hash in data/auth.json (mode 600), and signs
out every existing session.

    echo -n 'new password' | .venv/bin/python set_password.py [username]
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

from argon2 import PasswordHasher

DATA = Path(__file__).parent / "data"
user = sys.argv[1] if len(sys.argv) > 1 else "owner"
pw = sys.stdin.read().rstrip("\n")
if len(pw) < 8:
    sys.exit("password too short (min 8)")
DATA.mkdir(mode=0o700, exist_ok=True)
p = DATA / "auth.json"
fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump({"username": user, "password_hash": PasswordHasher().hash(pw)}, f)
db = DATA / "hud.db"
if db.exists():
    sqlite3.connect(db).execute("DELETE FROM sessions").connection.commit()
print(f"password set for '{user}'; all sessions signed out")
