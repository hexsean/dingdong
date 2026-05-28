FROM python:3.11-slim
ARG DINGDONG_VERSION=dev
LABEL org.opencontainers.image.title="dingdong" \
      org.opencontainers.image.version="${DINGDONG_VERSION}" \
      org.opencontainers.image.source="https://github.com/hexsean/dingdong"
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
VOLUME /app/data
ENTRYPOINT ["python", "-m", "dingdong"]
CMD ["start"]
