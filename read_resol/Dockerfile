FROM python:3.13-slim

RUN apt-get update && apt-get install -y \
    libmariadb-dev-compat build-essential curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip --quiet
RUN pip install --no-cache-dir -r requirements.txt

COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

CMD ["/bin/sh", "entrypoint.sh"]
