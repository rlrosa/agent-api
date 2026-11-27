#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
IMAGE_NAME="${OPENSHELL_IMAGE:-agent-api-sandbox:latest}"
OPENSHELL_CONFIG_DIR="${HOME}/.config/openshell"
OPENSHELL_TLS_DIR="${HOME}/.local/state/openshell/tls"
OPENSHELL_VERSION="${OPENSHELL_VERSION:-v0.1.10}"
OPENSHELL_VER_NUM="${OPENSHELL_VERSION#v}"

export PATH="${HOME}/.local/bin:${PATH}"

echo "==> [1/5] Checking / Installing NVIDIA OpenShell CLI (${OPENSHELL_VERSION}) in ~/.local/bin..."
if command -v openshell >/dev/null 2>&1; then
    echo "    openshell already installed at: $(command -v openshell)"
else
    mkdir -p "${HOME}/.local/bin" "${HOME}/.local/libexec/openshell"
    TMP_DIR="$(mktemp -d)"
    DEB_NAME="openshell_${OPENSHELL_VER_NUM}-1_amd64.deb"
    BASE_URL="https://github.com/ai-openshell/openshell/releases/download/${OPENSHELL_VERSION}"

    echo "    Downloading ${DEB_NAME} and checksums from ${BASE_URL}..."
    curl -fLsS -o "${TMP_DIR}/checksums.txt" "${BASE_URL}/openshell-checksums-sha256.txt"
    curl -fLsS -o "${TMP_DIR}/${DEB_NAME}" "${BASE_URL}/${DEB_NAME}"

    (cd "${TMP_DIR}" && grep " ${DEB_NAME}\$" checksums.txt | sha256sum -c -)
    dpkg-deb -x "${TMP_DIR}/${DEB_NAME}" "${TMP_DIR}/extracted"

    install -m 0755 "${TMP_DIR}/extracted/usr/bin/openshell" "${HOME}/.local/bin/openshell"
    install -m 0755 "${TMP_DIR}/extracted/usr/bin/openshell-gateway" "${HOME}/.local/bin/openshell-gateway"
    install -m 0755 "${TMP_DIR}/extracted/usr/bin/openshell-prover" "${HOME}/.local/bin/openshell-prover"
    if [ -f "${TMP_DIR}/extracted/usr/libexec/openshell/openshell-driver-vm" ]; then
        install -m 0755 "${TMP_DIR}/extracted/usr/libexec/openshell/openshell-driver-vm" \
            "${HOME}/.local/libexec/openshell/openshell-driver-vm"
    fi
    rm -rf "${TMP_DIR}"
    echo "    Installed openshell binaries into ${HOME}/.local/bin/"
fi

echo "==> [2/5] Ensuring OpenShell Gateway JWT signing bundle (${OPENSHELL_TLS_DIR})..."
if [ ! -f "${OPENSHELL_TLS_DIR}/jwt/signing.pem" ]; then
    mkdir -p "${OPENSHELL_TLS_DIR}"
    openshell-gateway generate-certs --output-dir "${OPENSHELL_TLS_DIR}"
fi

echo "==> [3/5] Configuring OpenShell Gateway schema v2 (${OPENSHELL_CONFIG_DIR}/gateway.toml)..."
mkdir -p "${OPENSHELL_CONFIG_DIR}"
cat << EOF > "${OPENSHELL_CONFIG_DIR}/gateway.toml"
[openshell]
version = 2

[openshell.gateway]
compute_driver = "docker"

[openshell.gateway.auth]
allow_unauthenticated_users = true

[openshell.gateway.gateway_jwt]
signing_key_path = "${OPENSHELL_TLS_DIR}/jwt/signing.pem"
public_key_path  = "${OPENSHELL_TLS_DIR}/jwt/public.pem"
kid_path         = "${OPENSHELL_TLS_DIR}/jwt/kid"
gateway_id       = "openshell"

[openshell.drivers.docker]
allow_driver_config = true
enable_bind_mounts = true

[openshell.drivers.docker.resource_admission]
enabled = false
EOF
openshell-gateway config preflight -- --disable-tls
echo "    Validated ${OPENSHELL_CONFIG_DIR}/gateway.toml (schema v2)"

echo "==> [4/5] Building sandbox image '${IMAGE_NAME}' from Dockerfile.sandbox..."
if command -v docker >/dev/null 2>&1; then
    docker build \
        --build-arg HOST_USER="$(id -un)" \
        --build-arg HOST_UID="$(id -u)" \
        --build-arg HOST_GID="$(id -g)" \
        -t "${IMAGE_NAME}" \
        -f "${REPO_ROOT}/Dockerfile.sandbox" \
        "${REPO_ROOT}"
else
    echo "    ERROR: docker is not installed or not on PATH."
    exit 1
fi

echo "==> [5/5] Verifying sandbox image and OpenShell CLI..."
docker run --rm "${IMAGE_NAME}" bash -c "which agy && agy --version || true; which claude && claude --version || true"
openshell --version
echo "==> Done."
