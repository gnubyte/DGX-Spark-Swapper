# Spark Swapper — web control plane for the DGX Spark's LLM servers.
#
#   docker build -t dgx-spark-swapper .
#   docker run -d --name spark-swapper --network host \
#     -v /var/run/docker.sock:/var/run/docker.sock \
#     -v /etc/spark-swapper:/config -v /var/lib/spark-swapper:/state \
#     dgx-spark-swapper
#
# --network host + the docker socket are what let it drive the model
# containers on the Spark. It generates a self-signed cert on first boot if
# /config/tls.crt is missing.

FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends openssl git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY swapper/ ./

ENV SWAPPER_CONF=/config \
    SWAPPER_STATE=/state \
    SWAPPER_IN_DOCKER=1 \
    SWAPPER_PORT=8999 \
    SWAPPER_BIND=0.0.0.0 \
    PYTHONUNBUFFERED=1

VOLUME ["/config", "/state"]
EXPOSE 8999

CMD ["python3", "app.py"]
