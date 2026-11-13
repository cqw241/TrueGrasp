"""Create an initial demo identity, without fabricating teaching results.

Run as root after content ingestion, then hand state files to the app user.
Credentials stay in a root-only file, never in source or console output.
"""

import json
import os
import secrets
from pathlib import Path

from app.auth import AuthStore
from app.config import get_settings
from app.contracts.entities import Student
from app.db import get_engine, init_db
from dotenv import load_dotenv
from sqlmodel import Session

load_dotenv("/etc/truegrasp/runtime.env", override=True)
os.umask(0o077)
get_settings.cache_clear()
engine = get_engine()
init_db(engine)
with Session(engine) as session:
    if session.get(Student, "A") is None:
        session.add(Student(id="A", name="演示学生 A"))
        session.commit()

accounts_path = Path("/etc/truegrasp/accounts.json")
if not accounts_path.exists():
    accounts = [
        {
            "username": "teacher",
            "role": "teacher",
            "student_id": None,
            "password": secrets.token_urlsafe(18),
        },
        {
            "username": "student-a",
            "role": "student",
            "student_id": "A",
            "password": secrets.token_urlsafe(18),
        },
    ]
    store = AuthStore(get_settings().auth_db_path)
    for account in accounts:
        store.put_account(
            account["username"],
            account["password"],
            account["role"],
            account["student_id"],
        )
    with accounts_path.open("x", encoding="utf-8") as handle:
        json.dump(
            {"url": "https://8.148.178.2", "accounts": accounts},
            handle,
            ensure_ascii=False,
            indent=2,
        )
    accounts_path.chmod(0o600)
    print("Created teacher and student accounts in a root-only credentials file.")
else:
    print("Existing credentials retained.")
