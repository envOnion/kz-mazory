"""Validate profile photos and store only normalized, metadata-free images."""

import logging
import re
import warnings
from io import BytesIO
from uuid import uuid4

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework import serializers

logger = logging.getLogger(__name__)
MAX_AVATAR_BYTES = 5 * 1024 * 1024
MAX_AVATAR_PIXELS = 20_000_000


def normalize_avatar(upload):
    if upload.size > MAX_AVATAR_BYTES:
        raise serializers.ValidationError("Фото должно быть не больше 5 МиБ.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(upload, formats=["JPEG", "PNG", "WEBP"]) as image:
                if image.width * image.height > MAX_AVATAR_PIXELS:
                    raise serializers.ValidationError(
                        "Фото должно содержать не больше 20 миллионов пикселей."
                    )
                if getattr(image, "n_frames", 1) != 1:
                    raise serializers.ValidationError("Выберите статичное фото без анимации.")
                image.verify()
            upload.seek(0)
            with Image.open(upload, formats=["JPEG", "PNG", "WEBP"]) as image:
                image.load()
                oriented = ImageOps.exif_transpose(image)
                side = min(512, *oriented.size)
                mode = "RGBA" if oriented.has_transparency_data else "RGB"
                cropped = ImageOps.fit(
                    oriented.convert(mode), (side, side), method=Image.Resampling.LANCZOS
                )
                # A fresh canvas deliberately excludes EXIF, ICC and XMP metadata.
                normalized = Image.new(mode, cropped.size)
                normalized.paste(cropped)
                output = BytesIO()
                normalized.save(output, format="WEBP", quality=85, method=4)
                return output.getvalue()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise serializers.ValidationError(
            "Фото должно содержать не больше 20 миллионов пикселей."
        )
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise serializers.ValidationError(
            "Не удалось прочитать фото. Выберите корректный JPEG, PNG или WebP."
        )


def avatar_storage():
    return FileSystemStorage(
        location=settings.AVATAR_ROOT,
        base_url=settings.AVATAR_URL,
        file_permissions_mode=0o644,
        directory_permissions_mode=0o755,
    )


def store_avatar(content):
    storage = avatar_storage()
    name = storage.save(f"{uuid4().hex}.webp", ContentFile(content))
    return storage.url(name)


def delete_avatar(url):
    # Legacy/external URLs and arbitrary paths can never delete a local file.
    match = re.fullmatch(re.escape(settings.AVATAR_URL) + r"([0-9a-f]{32}\.webp)", url)
    if match:
        try:
            avatar_storage().delete(match[1])
        except OSError:
            logger.exception("Could not remove obsolete profile avatar")
