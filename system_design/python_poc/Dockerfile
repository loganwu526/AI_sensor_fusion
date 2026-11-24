FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# 安裝最小系統相依套件（OpenCV Headless 與 ONNX Runtime 執行期函式庫）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libgomp1 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    python -c "import cv2, onnxruntime; assert hasattr(cv2, 'HOGDescriptor'), 'cv2 HOGDescriptor missing'"

# 複製應用程式、靜態頁面、預置與官方公開資料集及輕量化 ONNX 權重
COPY app ./app
COPY static ./static
COPY datasets ./datasets
COPY yolov8n-pose.onnx ./yolov8n-pose.onnx

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
