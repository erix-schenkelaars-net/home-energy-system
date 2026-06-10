FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --upgrade pip --quiet
RUN pip install --no-cache-dir -r requirements.txt
 
COPY battery_optimizer_LP_quarter.py ./
COPY pv_clearsky_profile*.json ./
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

CMD ["/bin/sh", "entrypoint.sh"]
