#!/bin/sh
# Download board dependencies once, before provisioning or updating.
set -eu
cd "$(dirname "$0")/.."

ONNX_VERSION="${ONNX_VERSION:-1.28.0}"
PLUGINS_VERSION="${PLUGINS_VERSION:-v3}"
PLUGINS_REPO="${PLUGINS_REPO:-pollen-robotics/microduck-gst-plugins}"
MPP_VERSION="${MPP_VERSION:-1.5.0-1}"
RGA_VERSION="${RGA_VERSION:-2.2.0-1}"
RUNTIME=v2.3.2
POOL="${RADXA_POOL:-https://radxa-repo.github.io/bullseye/pool/main}"
mkdir -p deploy
failed=0
while read -r name url; do
    if [ -s "deploy/$name" ]; then
        printf '==> cached: %s\n' "$name"
        continue
    fi
    printf '==> downloading: %s\n    %s\n' "$name" "$url"
    if curl -fL --progress-bar --connect-timeout 15 --max-time 300 \
        --speed-limit 1024 --speed-time 60 -o "deploy/$name.part" "$url"; then
        mv "deploy/$name.part" "deploy/$name"
    else
        rm -f "deploy/$name.part"
        printf 'Download failed; put this file in deploy/: %s\n' "$name" >&2
        failed=1
    fi
done <<EOF
onnxruntime-linux-aarch64-${ONNX_VERSION}.tgz https://github.com/microsoft/onnxruntime/releases/download/v${ONNX_VERSION}/onnxruntime-linux-aarch64-${ONNX_VERSION}.tgz
microduck-gst-plugins-${PLUGINS_VERSION}-aarch64.tar.gz https://github.com/${PLUGINS_REPO}/releases/download/${PLUGINS_VERSION}/microduck-gst-plugins-${PLUGINS_VERSION}-aarch64.tar.gz
microduck-gst-plugins-${PLUGINS_VERSION}-aarch64.tar.gz.sha256 https://github.com/${PLUGINS_REPO}/releases/download/${PLUGINS_VERSION}/microduck-gst-plugins-${PLUGINS_VERSION}-aarch64.tar.gz.sha256
librockchip-mpp1_${MPP_VERSION}_arm64.deb ${POOL}/m/mpp/librockchip-mpp1_${MPP_VERSION}_arm64.deb
librga2_${RGA_VERSION}_arm64.deb ${POOL}/libr/librga/librga2_${RGA_VERSION}_arm64.deb
camera_engine_rkaiq_rk3568_arm64-fixed.deb ${POOL}/c/camera-engine-rkaiq/camera_engine_rkaiq_rk3568_arm64-fixed.deb
rockchip-iqfiles-rk356x_0.1.16_all.deb ${POOL}/r/rockchip-iqfiles/rockchip-iqfiles-rk356x_0.1.16_all.deb
librknnrt-${RUNTIME}.so https://raw.githubusercontent.com/airockchip/rknn-toolkit2/${RUNTIME}/rknpu2/runtime/Linux/librknn_api/aarch64/librknnrt.so
EOF
[ "$failed" = 0 ] || exit 1
(cd deploy && sha256sum -c "microduck-gst-plugins-${PLUGINS_VERSION}-aarch64.tar.gz.sha256")
printf '==> board dependencies ready in %s/deploy\n' "$PWD"
