FROM debian:12-slim

# python3-eccodes (Debian) pulls ecmwflibs, which calls dpkg-architecture at import
# time -> dpkg-dev provides it. ca-certificates for HTTPS to the KNMI API + S3.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-eccodes python3-numpy python3-requests python3-dotenv \
      python3-pip dpkg-dev tzdata ca-certificates \
      libeccodes0 libeccodes-data \
    && pip install --break-system-packages --no-cache-dir mysql-connector-python \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/Amsterdam
WORKDIR /app
# read_knmi.py is bind-mounted (.:/app) — edit + restart, no rebuild needed.
CMD ["python3", "read_knmi.py"]
