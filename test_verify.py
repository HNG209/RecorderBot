import sys
import tempfile
import os
from pathlib import Path

# Đảm bảo stdout hỗ trợ utf-8 trên Windows console
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient
import main
from app.config import settings
from app.r2_storage import r2_storage
from app.recorder import recorder_manager, RoomRecorderBot

def test_app():
    print("[TEST] Testing FastAPI app routes...")
    client = TestClient(main.app)

    # 1. Health check
    res = client.get("/health")
    assert res.status_code == 200, f"Health check failed: {res.text}"
    print("[PASS] GET /health ->", res.json())

    # 2. Storage status
    res = client.get("/storage/r2/status")
    assert res.status_code == 200, f"Storage status failed: {res.text}"
    print("[PASS] GET /storage/r2/status ->", res.json())

    # 3. Active recordings
    res = client.get("/recordings/active")
    assert res.status_code == 200, f"Active recordings failed: {res.text}"
    print("[PASS] GET /recordings/active ->", res.json())

    # 4. Stop when not recording -> 404
    res = client.post("/recordings/stop", json={"room_name": "non_existent_room"})
    assert res.status_code == 404, f"Expected 404 for non-existent room: {res.text}"
    print("[PASS] POST /recordings/stop (non-existent room handled with 404)")

    # 5. Room status
    res = client.get("/recordings/test-room/status")
    assert res.status_code == 200
    print("[PASS] GET /recordings/test-room/status ->", res.json())

    # 6. Test R2 upload directly
    print("[TEST] Testing Cloudflare R2 upload...")
    if r2_storage.is_available:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            test_file = tmp_path / "test_ping.txt"
            test_file.write_text("LiveKit Recorder Bot - Cloudflare R2 Test Ping", encoding="utf-8")
            
            upload_res = r2_storage.upload_file(test_file, "tests/test_ping.txt")
            print("[PASS] Cloudflare R2 upload result:", upload_res)
            assert upload_res is not None, "Failed to upload test file to R2"
            assert upload_res["object_key"] == "tests/test_ping.txt"

    # 7. Test RoomRecorderBot structure
    print("[TEST] Testing RoomRecorderBot instantiation...")
    bot = RoomRecorderBot("test-room-123", session_id="test_session")
    assert bot.room_name == "test-room-123"
    assert bot.session_id == "test_session"
    assert bot.output_dir.exists()
    print("[PASS] RoomRecorderBot initialized correctly")

    print("\n[ALL TESTS PASSED SUCCESSFULLY!]")

if __name__ == "__main__":
    test_app()
