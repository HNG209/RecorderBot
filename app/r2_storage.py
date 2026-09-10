import asyncio
import os
import mimetypes
from pathlib import Path
from typing import List, Dict, Any, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import settings, logger


class R2StorageService:
    """Service xử lý kết nối và upload file lên Cloudflare R2 (S3-compatible)."""

    def __init__(self):
        self._client = None
        self._init_client()

    def _init_client(self) -> None:
        if not settings.is_r2_configured:
            logger.warning(
                "Cloudflare R2 chưa được cấu hình đầy đủ trong .env. "
                "Các thao tác upload sẽ bị bỏ qua."
            )
            return

        try:
            self._client = boto3.client(
                "s3",
                endpoint_url=settings.R2_ENDPOINT_URL,
                aws_access_key_id=settings.R2_ACCESS_KEY_ID,
                aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
                region_name="auto",
                config=Config(
                    signature_version="s3v4",
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
            logger.info(
                "Cloudflare R2 client đã khởi tạo thành công. Bucket: %s | Endpoint: %s",
                settings.R2_BUCKET_NAME,
                settings.R2_ENDPOINT_URL,
            )
        except Exception as e:
            logger.exception("Không thể khởi tạo Cloudflare R2 client: %s", e)
            self._client = None

    @property
    def is_available(self) -> bool:
        return self._client is not None

    def get_public_url(self, object_key: str) -> Optional[str]:
        """Tạo public URL nếu R2_PUBLIC_URL được cấu hình."""
        if settings.R2_PUBLIC_URL:
            base = settings.R2_PUBLIC_URL.rstrip("/")
            key = object_key.lstrip("/")
            return f"{base}/{key}"
        return None

    def generate_presigned_url(self, object_key: str, expires_in: int = 3600) -> Optional[str]:
        """Tạo Presigned URL tải file từ R2 (hết hạn sau `expires_in` giây)."""
        if not self.is_available:
            return None
        try:
            url = self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.R2_BUCKET_NAME, "Key": object_key},
                ExpiresIn=expires_in,
            )
            return url
        except Exception as e:
            logger.error("Lỗi tạo presigned URL cho %s: %s", object_key, e)
            return None

    async def upload_file(self, local_path: Path, object_key: str) -> Optional[Dict[str, Any]]:
        """Upload một file đơn lẻ lên Cloudflare R2."""
        if not self.is_available:
            logger.warning("Bỏ qua upload %s do R2 chưa được cấu hình.", local_path.name)
            return None

        if not local_path.is_file():
            logger.error("File không tồn tại: %s", local_path)
            return None

        content_type, _ = mimetypes.guess_type(str(local_path))
        if not content_type:
            suffix = local_path.suffix.lower()
            if suffix == ".webm":
                content_type = "video/webm"
            elif suffix == ".wav":
                content_type = "audio/wav"
            elif suffix == ".json":
                content_type = "application/json"
            else:
                content_type = "application/octet-stream"

        file_size = local_path.stat().st_size
        logger.info(
            "Bắt đầu upload file lên R2: %s (%.2f KB) -> key: %s",
            local_path.name,
            file_size / 1024,
            object_key,
        )

        try:
            extra_args = {"ContentType": content_type}
            # boto3.upload_file() là blocking synchronous I/O.
            # Offload sang thread pool để không block asyncio event loop.
            loop = asyncio.get_event_loop()
            _client = self._client
            _bucket = settings.R2_BUCKET_NAME
            _filename = str(local_path)

            await loop.run_in_executor(
                None,
                lambda: _client.upload_file(
                    Filename=_filename,
                    Bucket=_bucket,
                    Key=object_key,
                    ExtraArgs=extra_args,
                ),
            )
            logger.info("Upload thành công: %s -> %s", local_path.name, object_key)

            public_url = self.get_public_url(object_key)
            return {
                "filename": local_path.name,
                "object_key": object_key,
                "size_bytes": file_size,
                "content_type": content_type,
                "public_url": public_url,
            }
        except ClientError as e:
            logger.error("ClientError khi upload %s lên R2: %s", local_path.name, e)
            return None
        except Exception as e:
            logger.exception("Lỗi không xác định khi upload %s lên R2: %s", local_path.name, e)
            return None

    async def upload_directory(self, dir_path: Path, prefix: str = "") -> List[Dict[str, Any]]:
        """Upload toàn bộ file thô trong thư mục lên R2 với prefix tương ứng."""
        if not self.is_available:
            logger.warning("Bỏ qua upload thư mục %s do R2 chưa cấu hình.", dir_path)
            return []

        if not dir_path.exists() or not dir_path.is_dir():
            logger.error("Thư mục không tồn tại: %s", dir_path)
            return []

        uploaded_results = []
        prefix = prefix.strip("/")

        # Quét tất cả file trong thư mục dir_path
        for file_path in dir_path.rglob("*"):
            if file_path.is_file():
                rel_path = file_path.relative_to(dir_path).as_posix()
                object_key = f"{prefix}/{rel_path}" if prefix else rel_path

                result = await self.upload_file(file_path, object_key)
                if result:
                    uploaded_results.append(result)

        logger.info(
            "Đã hoàn thành upload %d files từ thư mục %s lên R2.",
            len(uploaded_results),
            dir_path,
        )
        return uploaded_results


# Singleton instance
r2_storage = R2StorageService()
