import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "local-development-only")
    UPLOAD_FOLDER = PROJECT_ROOT / "uploads"
    MAX_CONTENT_LENGTH = 25 * 1024 * 1024
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        f"sqlite:///{PROJECT_ROOT / 'instance' / 'aircraft_archive.db'}",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
