# liqoctl is baked in because the operator drives peering by invoking it:
# `liqoctl peer` is three phases that each need both kubeconfigs open at once,
# and reimplementing the WireGuard key exchange and Tenant/Identity CSR flow in
# Python would buy nothing but bugs.
#
# LIQO_VERSION must match the hub's Liqo chart AND the version each machine
# installs in cloud-init. All three are one number; keep them together.
ARG LIQO_VERSION=v1.2.0

# Runs on the BUILD machine, not the target: this stage only downloads, so
# there is no reason to emulate the target arch to do it.
FROM --platform=$BUILDPLATFORM alpine:3.21 AS liqoctl
ARG LIQO_VERSION
# Set by BuildKit from the platform being built (amd64 locally on Apple silicon
# is arm64; CI pins linux/amd64). Liqo publishes both under these exact names.
ARG TARGETARCH
# The image redistributes liqoctl, so Apache-2.0 4(a) wants the license shipped
# with it and the release tarball holds only the binary. Same tag, so the text
# matches what it ships next to. (Liqo has no NOTICE file; 4(d) does not apply.)
RUN apk add --no-cache curl tar \
 && arch="${TARGETARCH:-amd64}" \
 && curl -sfL "https://github.com/liqotech/liqo/releases/download/${LIQO_VERSION}/liqoctl-linux-${arch}.tar.gz" \
    | tar -xz -C /usr/local/bin liqoctl \
 && chmod 0755 /usr/local/bin/liqoctl \
 && mkdir -p /licenses/liqo \
 && curl -sfL "https://raw.githubusercontent.com/liqotech/liqo/${LIQO_VERSION}/LICENSE" \
    -o /licenses/liqo/LICENSE

FROM python:3.12-slim

WORKDIR /app

# Core dependencies first, so editing the operator does not re-resolve them.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY --from=liqoctl /usr/local/bin/liqoctl /usr/local/bin/liqoctl
COPY --from=liqoctl /licenses/ /licenses/
COPY autoscaler/ /app/autoscaler/

# Provider dependencies are discovered, not listed. Adding a node source means
# adding providers/<name>/requirements.txt and nothing else — this file, the
# chart and the core stay untouched.
RUN set -eu; \
    for f in /app/autoscaler/providers/*/requirements.txt; do \
      [ -e "$f" ] || continue; \
      echo "installing provider deps from $f"; \
      pip install --no-cache-dir -r "$f"; \
    done

# Matches runAsUser in the chart. `nobody` is 65534 on debian, so create one.
RUN useradd --uid 65532 --create-home --shell /usr/sbin/nologin nonroot
USER 65532

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "-m", "autoscaler"]
