FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared shared
COPY services services
COPY gateway gateway
COPY mocks mocks
COPY api api
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
