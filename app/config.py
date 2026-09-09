import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# Tải cấu hình từ .env
load_dotenv()

class Settings:
    # LiveKit Configuration
    LIVEKIT_URL: str = os.getenv("LIVEKIT_URL", "ws://localhost:7880")
    LIVEKIT_API_KEY: str = os.getenv("LIVEKIT_API_KEY", "devkey")
    LIVEKIT_API_SECRET: str = os.getenv("LIVEKIT_API_SECRET", "secret")

    # Cloudflare R2 Storage Configuration
    R2_ACCOUNT_ID: str = os.getenv("R2_ACCOUNT_ID", "")
    R2_ACCESS_KEY_ID: str = os.getenv("R2_ACCESS_KEY_ID", "")
    R2_SECRET_ACCESS_KEY: str = os.getenv("R2_SECRET_ACCESS_KEY", "")
    R2_BUCKET_NAME: str = os.getenv("R2_BUCKET_NAME", "")
    _R2_ENDPOINT_URL: str = os.getenv("R2_ENDPOINT_URL", "")
    R2_PUBLIC_URL: str = os.getenv("R2_PUBLIC_URL", "")

    # Local Directory Configuration
    RECORDINGS_DIR: Path = Path(os.getenv("RECORDINGS_DIR", "recordings"))

    # Server Configuration
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Webhook Configuration
    WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "")

    @property
    def R2_ENDPOINT_URL(self) -> str:
        """Trả về endpoint URL cho Cloudflare R2."""
        if self._R2_ENDPOINT_URL:
            return self._R2_ENDPOINT_URL
        if self.R2_ACCOUNT_ID:
            return f"https://{self.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
        return ""

    @property
    def is_r2_configured(self) -> bool:
        """Kiểm tra xem Cloudflare R2 đã được cấu hình đầy đủ chưa."""
        return bool(
            self.R2_ACCESS_KEY_ID
            and self.R2_SECRET_ACCESS_KEY
            and self.R2_BUCKET_NAME
            and (self.R2_ACCOUNT_ID or self._R2_ENDPOINT_URL)
        )

    @property
    def is_webhook_configured(self) -> bool:
        """Kiểm tra xem Webhook URL đã được cấu hình chưa."""
        return bool(self.WEBHOOK_URL and self.WEBHOOK_URL.strip())

settings = Settings()

# Thiết lập logging chung
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("recorder-bot")
