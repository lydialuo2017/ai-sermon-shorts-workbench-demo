FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# AutoClip engine pinned to the exact commit used locally (0.1.0.dev0).
RUN pip install --no-cache-dir "git+https://github.com/artbyjazi/autoclip@5d0eac36fa615b79dd2104083bf273a96f8d68bb" Pillow
COPY server.py pipeline_wb.py ./
COPY static ./static
COPY tasks ./tasks
EXPOSE 8901
CMD ["sh", "-c", "python server.py ${PORT:-8901}"]
