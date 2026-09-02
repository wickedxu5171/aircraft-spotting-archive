from pathlib import Path
from uuid import uuid4


class InvalidPhotoError(ValueError):
    pass


def _image_extension(header: bytes) -> str | None:
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    return None


def save_photo_upload(file_storage, upload_folder: str | Path) -> str:
    """Validate an uploaded image and store it under a generated local name."""
    if file_storage is None or not file_storage.filename:
        raise InvalidPhotoError("Choose a JPEG, PNG or WebP photo to upload.")

    header = file_storage.stream.read(16)
    file_storage.stream.seek(0)
    extension = _image_extension(header)
    if extension is None:
        raise InvalidPhotoError("Only genuine JPEG, PNG and WebP images are accepted.")

    destination = Path(upload_folder)
    destination.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid4().hex}{extension}"
    file_storage.save(destination / stored_name)
    return stored_name
