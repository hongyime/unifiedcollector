FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY services/face_recognition/requirements.txt /app/requirements.txt
# Prefer binary wheels to avoid compiling dlib from source (~30 min).
# Falls back to source if no wheel matches the platform.
ENV CMAKE_BUILD_PARALLEL_LEVEL=4
RUN pip install --no-cache-dir --prefer-binary -r /app/requirements.txt

# face_recognition_models uses pkg_resources which setuptools 70+ no longer exposes
# as a top-level module. Patch it to use os.path instead.
COPY services/face_recognition/patch_frm.py /tmp/patch_frm.py
RUN python3 /tmp/patch_frm.py

COPY shared /app/shared
RUN pip install --no-cache-dir -e /app/shared

COPY services/face_recognition /app

CMD ["python", "main.py"]
