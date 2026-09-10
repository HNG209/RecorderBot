"""
post_process.py - Test helper ghép audio + screen từ timeline.json bằng ffmpeg.

Đầu vào: thư mục recording chứa timeline.json + các file .wav / .webm
Đầu ra: output.mp4 trong cùng thư mục recording

Cách dùng:
    python post_process.py recordings/meet-xxx/session_id/rec_randomId
    python post_process.py recordings/meet-xxx/session_id/rec_randomId --out result.mp4
VD: python post_process.py recordings\meet-6a80-ii70qip\string6\rec_8a1f2c3d --out merged.mp4
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def get_real_duration(filepath: Path) -> float | None:
    """Dùng ffprobe lấy duration thực tế của file media."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(filepath),
            ],
            capture_output=True, text=True, check=True,
        )
        val = float(result.stdout.strip())
        return val if val > 0 else None
    except Exception:
        return None


def build_ffmpeg_cmd(
    session_dir: Path,
    timeline: dict,
    output_path: Path,
) -> list[str]:
    """
    Xây dựng lệnh ffmpeg ghép audio + screen segments theo logic tương tự NestJS.

    Logic:
    - Dùng 1 nền đen (lavfi color) làm base video.
    - Overlay từng đoạn screen lên đúng thời điểm start/end của nó.
    - Mix audio từ tất cả các track (amerge nếu nhiều người, amix/copy nếu 1 người).
    - global_t0 = min(tất cả audio.start + screen.start) → mốc t=0 tuyệt đối.
    - Mỗi track (audio/screen) đều tính offset = track.start - global_t0.
    """
    audio_segments = timeline.get("audio_segments", [])
    screen_segments = timeline.get("screen_segments", [])

    if not audio_segments:
        print("[ERROR] Không tìm thấy audio segment trong timeline.json")
        sys.exit(1)

    # Lấy điểm bắt đầu sớm nhất trong TẤT CẢ các track (audio + screen) làm mốc t=0.
    # Điều này đảm bảo dù audio hay screen bắt đầu trước, timeline vẫn luôn khớp.
    all_starts = [seg["start"] for seg in audio_segments] + [
        seg["start"] for seg in screen_segments if seg.get("file")
    ]
    global_t0 = min(all_starts) if all_starts else 0.0
    print(f"[INFO] global_t0    : {global_t0:.3f}s")

    # --- Build input list ---
    # Input 0: nền đen
    cmd = ["ffmpeg", "-y"]
    cmd += ["-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=30"]

    # Input 1..N: các file audio WAV
    audio_input_indices = []
    # current_input_idx theo dõi index ffmpeg input thực tế (0 = nền đen)
    current_input_idx = 1
    for seg in audio_segments:
        audio_file = session_dir / seg["file"]
        if not audio_file.exists():
            print(f"[WARN] File audio không tồn tại: {audio_file}, bỏ qua.")
            continue
        cmd += ["-i", str(audio_file)]
        audio_input_indices.append((current_input_idx, seg))
        current_input_idx += 1

    # Input N+1..M: các file screen WebM
    screen_input_indices = []
    for seg in screen_segments:
        if not seg.get("file"):
            continue
        screen_file = session_dir / seg["file"]
        if not screen_file.exists():
            print(f"[WARN] File screen không tồn tại: {screen_file}, bỏ qua.")
            continue

        # Lấy duration thực từ ffprobe (chính xác hơn timeline)
        real_dur = get_real_duration(screen_file)
        # start_offset tính từ global_t0 (mốc sớm nhất của toàn session)
        start_offset = max(0.0, seg["start"] - global_t0)
        if real_dur:
            end_offset = start_offset + real_dur
        else:
            end_offset = max(start_offset, seg["end"] - global_t0)

        cmd += ["-i", str(screen_file)]
        # Lưu current_input_idx thực tế (không dùng base_idx + i vì có thể bị skip)
        screen_input_indices.append((current_input_idx, seg, start_offset, end_offset))
        current_input_idx += 1

    if not screen_input_indices and not audio_input_indices:
        print("[ERROR] Không có file media hợp lệ nào để ghép.")
        sys.exit(1)

    # --- Build filter_complex ---
    filter_parts = []
    last_video_out = "0:v"  # base: nền đen

    # Overlay từng đoạn screen
    for idx, seg, start_offset, end_offset in screen_input_indices:
        shifted_label = f"shifted{idx}"
        overlay_label = f"vout{idx}"

        # Scale + pad về 1920x1080, rồi dịch PTS về đúng vị trí thời gian.
        # Luôn dùng (PTS-STARTPTS) để reset về 0 trước (dù webm bắt đầu lúc nào),
        # sau đó mới cộng start_offset để đặt vào đúng vị trí trên timeline.
        filter_parts.append(
            f"[{idx}:v]"
            f"scale=1920:1080:force_original_aspect_ratio=decrease,"
            f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,"
            f"setpts=(PTS-STARTPTS)+{start_offset}/TB"
            f"[{shifted_label}]"
        )
        filter_parts.append(
            f"[{last_video_out}][{shifted_label}]"
            f"overlay=x=0:y=0:enable='between(t,{start_offset},{end_offset})'"
            f"[{overlay_label}]"
        )
        last_video_out = overlay_label

    # Delay + mix audio
    # Mỗi audio track cần được delay đúng vị trí trên timeline (tính từ global_t0).
    delayed_audio_labels = []
    for idx, seg in audio_input_indices:
        audio_delay_ms = max(0.0, (seg["start"] - global_t0) * 1000)
        delayed_label = f"adelayed{idx}"
        if audio_delay_ms > 0:
            # adelay={ms}:all=1 → delay tất cả channels đồng đều
            filter_parts.append(
                f"[{idx}:a]adelay={audio_delay_ms:.0f}:all=1[{delayed_label}]"
            )
            delayed_audio_labels.append(f"[{delayed_label}]")
        else:
            # Không cần delay, dùng trực tiếp
            delayed_audio_labels.append(f"[{idx}:a]")

    if len(delayed_audio_labels) > 1:
        audio_inputs_str = "".join(delayed_audio_labels)
        filter_parts.append(
            f"{audio_inputs_str}amix=inputs={len(delayed_audio_labels)}:duration=longest[aout]"
        )
        audio_map = "[aout]"
    elif len(delayed_audio_labels) == 1:
        label = delayed_audio_labels[0]
        if label.startswith("[") and label.endswith("]") and "adelayed" in label:
            # Đã đi qua adelay filter → dùng label
            audio_map = label
        elif label.startswith("[") and label.endswith("]"):
            # Dạng [idx:a] → dùng trực tiếp, không qua filter
            # Cần tách ra: map trực tiếp không cần filter_complex
            audio_map = f"{audio_input_indices[0][0]}:a"
        else:
            audio_map = label
    else:
        audio_map = None

    # --- Assemble final command ---
    if filter_parts:
        cmd += ["-filter_complex", ";".join(filter_parts)]
        cmd += ["-map", f"[{last_video_out}]"]
    else:
        cmd += ["-map", "0:v"]

    if audio_map:
        cmd += ["-map", audio_map]

    cmd += [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-r", "30",
        "-crf", "23",
        "-c:a", "aac",
        "-shortest",
        str(output_path),
    ]

    return cmd


def main():
    parser = argparse.ArgumentParser(
        description="Test ghép audio + screen từ timeline.json bằng ffmpeg"
    )
    parser.add_argument("session_dir", help="Đường dẫn tới thư mục session (chứa timeline.json)")
    parser.add_argument("--out", default="output.mp4", help="Tên file output (default: output.mp4)")
    args = parser.parse_args()

    session_dir = Path(args.session_dir)
    timeline_path = session_dir / "timeline.json"

    if not timeline_path.exists():
        print(f"[ERROR] Không tìm thấy timeline.json trong {session_dir}")
        sys.exit(1)

    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    output_path = session_dir / args.out

    print(f"[INFO] Session dir : {session_dir}")
    print(f"[INFO] Room        : {timeline.get('room')}")
    print(f"[INFO] Duration    : {timeline.get('duration_sec')}s")
    print(f"[INFO] Audio segs  : {len(timeline.get('audio_segments', []))}")
    print(f"[INFO] Screen segs : {len(timeline.get('screen_segments', []))}")
    print(f"[INFO] Output      : {output_path}")
    print()

    cmd = build_ffmpeg_cmd(session_dir, timeline, output_path)

    print("[CMD]", " ".join(cmd))
    print()

    try:
        subprocess.run(cmd, check=True)
        print(f"\n[OK] Ghép xong! File output: {output_path}")
    except subprocess.CalledProcessError as e:
        print(f"\n[ERROR] ffmpeg thất bại với exit code {e.returncode}")
        sys.exit(e.returncode)
    except FileNotFoundError:
        print("[ERROR] Không tìm thấy lệnh 'ffmpeg'. Vui lòng cài ffmpeg và thêm vào PATH.")
        sys.exit(1)


if __name__ == "__main__":
    main()
