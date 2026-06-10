FROM python:3.13-slim

WORKDIR .

COPY requirements.txt .
RUN pip install --upgrade pip --quiet
RUN pip install --no-cache-dir -r requirements.txt

COPY ha_p60_data_2.py .

COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

CMD ["/bin/sh", "entrypoint.sh"]
