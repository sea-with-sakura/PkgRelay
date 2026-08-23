FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY client ./client
COPY config.example.yaml ./config.example.yaml

ENV PKGRELAY_CONFIG=/config/config.yaml
VOLUME ["/data", "/config"]
EXPOSE 8080
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
