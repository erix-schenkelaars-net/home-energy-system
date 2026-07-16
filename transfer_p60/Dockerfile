FROM python:3.13-slim

# /app like every other service: `WORKDIR .` resolved to / and put the script outside the
# ../common:/app/common mount, so `from common import ...` could not resolve.
WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip --quiet
RUN pip install --no-cache-dir -r requirements.txt

COPY ha_p60_data_2.py .

COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

CMD ["/bin/sh", "entrypoint.sh"]
