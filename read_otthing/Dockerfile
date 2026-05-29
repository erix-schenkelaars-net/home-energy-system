FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip --quiet
RUN pip install --no-cache-dir -r requirements.txt

COPY read_otthing_pub_wip*.py .
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

CMD ["/bin/sh", "entrypoint.sh"]
