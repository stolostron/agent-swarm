FROM registry.access.redhat.com/ubi10/python-312-minimal:latest

ARG AGENT_IMAGE_OPENCODE
ENV AGENT_IMAGE_OPENCODE=${AGENT_IMAGE_OPENCODE}

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY swarmer/ swarmer/

# Create mount point directories as root (base image runs as uid 1001)
# Note: PVC mounts overlay /data at runtime; ensure the PVC root is group-0
# writable (chgrp -R 0 /data on the PVC) for uid 1001 + gid 0 write access.
USER 0
RUN mkdir -p /data /auth
USER 1001

ENV PYTHONUNBUFFERED=1 \
    K8S_IN_CLUSTER=true \
    AUTH_HASH_FILE=/auth/password.hash \
    DATABASE_URL=sqlite+aiosqlite:////data/swarmer.db

EXPOSE 8080

CMD ["uvicorn", "swarmer.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips=*"]
