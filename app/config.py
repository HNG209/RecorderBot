import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# Tải cấu hình từ .env
load_dotenv()

# Video Presets cho GStreamer Screen Recording
VIDEO_PRESETS = {
    "720p": {"width": 1280, "height": 720, "bitrate": 2_500_000, "cpu_used": 6},
    "1080p": {"width": 1920, "height": 1080, "bitrate": 5_000_000, "cpu_used": 4},
}

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
    CLEANUP_LOCAL_AFTER_UPLOAD: bool = os.getenv(
        "CLEANUP_LOCAL_AFTER_UPLOAD", "true"
    ).lower() in ("true", "1", "yes")

    # Server Configuration
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Webhook Configuration
    WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "")

    # Video Recording Configuration (GStreamer)
    VIDEO_PRESET: str = os.getenv("VIDEO_PRESET", "1080p")

    @property
    def video_preset_config(self) -> dict:
        """Lấy cấu hình preset video. Mặc định 1080p nếu preset không tồn tại."""
        preset_key = self.VIDEO_PRESET.lower()
        if preset_key in VIDEO_PRESETS:
            return VIDEO_PRESETS[preset_key]
        return VIDEO_PRESETS["1080p"]

    @property
    def VIDEO_WIDTH(self) -> int:
        return int(os.getenv("VIDEO_WIDTH", self.video_preset_config["width"]))

    @property
    def VIDEO_HEIGHT(self) -> int:
        return int(os.getenv("VIDEO_HEIGHT", self.video_preset_config["height"]))

    @property
    def VIDEO_BITRATE(self) -> int:
        return int(os.getenv("VIDEO_BITRATE", self.video_preset_config["bitrate"]))

    @property
    def VIDEO_CPU_USED(self) -> int:
        return int(os.getenv("VIDEO_CPU_USED", self.video_preset_config["cpu_used"]))

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
