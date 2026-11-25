FROM nousresearch/hermes-agent:v0.23.3@sha256:3e9fe4c6ef36490f43aa0d0d80f9df64e9c92a576eee3042f1a97c8813c05355

RUN apt-get -o Acquire::Retries=3 update \
    && apt-get install -y --no-install-recommends nginx-light \
    && rm -rf /var/lib/apt/lists/*

COPY config.yaml /opt/doorbell-hermes/config.yaml
COPY --chmod=0755 init-config.sh /etc/cont-init.d/00-doorbell-config
COPY voice_gateway.py nginx.conf /opt/doorbell-hermes/
COPY --chmod=0755 run-http /etc/services.d/doorbell-http/run
COPY --chmod=0755 run-voice /etc/services.d/doorbell-voice/run

ENV API_SERVER_ENABLED=true \
    API_SERVER_HOST=127.0.0.1 \
    API_SERVER_PORT=10001

EXPOSE 10000
CMD ["gateway", "run"]
