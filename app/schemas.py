from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class StartRecordingRequest(BaseModel):
    room_name: str = Field(..., description="Tên phòng LiveKit cần ghi âm / quay màn hình")
    session_id: Optional[str] = Field(None, description="Mã định danh phiên ghi (tuỳ chọn, mặc định tạo theo timestamp)")


class StartRecordingResponse(BaseModel):
    success: bool
    message: str
    room_name: str
    session_id: str
    output_dir: str


class StopRecordingRequest(BaseModel):
    room_name: str = Field(..., description="Tên phòng LiveKit cần dừng ghi")


class UploadedFileItem(BaseModel):
    filename: str
    object_key: str
    size_bytes: int
    public_url: Optional[str] = None


class StopRecordingResponse(BaseModel):
    success: bool
    message: str
    room_name: str
    session_id: str
    duration_sec: float
    local_output_dir: str
    timeline: Dict[str, Any]
    uploaded_files: List[UploadedFileItem]


class RecordingStatusItem(BaseModel):
    room_name: str
    session_id: str
    status: str
    duration_sec: float
    output_dir: str
    audio_segments_count: int
    screen_segments_count: int


class ActiveRecordingsResponse(BaseModel):
    total_active: int
    recordings: List[RecordingStatusItem]


class R2ConfigStatusResponse(BaseModel):
    configured: bool
    bucket_name: Optional[str] = None
    endpoint_url: Optional[str] = None
    public_url_configured: bool
