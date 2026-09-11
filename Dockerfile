FROM python:3.12-slim

# Cài GStreamer + dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-gi \
    python3-gi-cairo \
    gir1.2-gstreamer-1.0 \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    libgirepository1.0-dev \
    libcairo2-dev \
    pkg-config \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cài Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy toàn bộ source code
COPY . .

# Tạo thư mục recordings
RUN mkdir -p /app/recordings

EXPOSE 8000

# main.py nằm ở root
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]