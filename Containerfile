FROM registry.access.redhat.com/ubi10/python-312-minimal:latest

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# OpenShell executes the sandbox supervisor as UID 1000 even when the image's
# default user differs. Keep the installed Python runtime readable there while
# preserving the non-root default user for Kubernetes deployments.
USER 0
RUN chmod -R a+rX /opt/app-root

# Copy application
COPY swarmer/ swarmer/

# Create mount point directories as root (base image runs as uid 1001)
# Note: PVC mounts overlay /data at runtime; ensure the PVC root is group-0
# writable (chgrp -R 0 /data on the PVC) for uid 1001 + gid 0 write access.
USER 0
RUN microdnf install -y iproute util-linux-core tar && microdnf clean all && \
    mkdir -p /data /auth /sandbox/auth && chmod 0777 /sandbox /sandbox/auth
USER 1001

ENV PYTHONUNBUFFERED=1 \
    K8S_IN_CLUSTER=true \
    AUTH_HASH_FILE=/auth/password.hash \
    DATABASE_URL=sqlite+aiosqlite:////data/swarmer.db

EXPOSE 8080

CMD ["uvicorn", "swarmer.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips=*"]
