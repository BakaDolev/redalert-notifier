FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends bc && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY listener.py healthcheck.sh ./
RUN chmod +x healthcheck.sh

VOLUME /app/session

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=30s \
    CMD /app/healthcheck.sh

CMD ["python", "-u", "listener.py"]
