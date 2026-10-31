#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

as_root() {
  if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi
}

# Only host tools: cross-sysroot.sh downloads the ARM libraries into the checkout.
packages=(git openssh-client curl ca-certificates build-essential pkg-config cmake binutils
  python3-venv libudev-dev libdbus-1-dev libssl-dev xz-utils zstd unzip file
  avahi-daemon libnss-mdns)
missing=()
for package in "${packages[@]}"; do
  [ "$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)" = installed ] || missing+=("$package")
done
if [ "${#missing[@]}" -gt 0 ]; then
  as_root apt-get -o Acquire::Retries=3 update
  if grep -Eq '^ID="?ubuntu"?$' /etc/os-release; then
    as_root apt-get install -y --no-install-recommends software-properties-common
    as_root add-apt-repository -y universe
    as_root apt-get -o Acquire::Retries=3 update
  fi
  as_root apt-get -o Acquire::Retries=3 install -y --no-install-recommends "${missing[@]}"
fi

export RUSTUP_DIST_SERVER="${RUSTUP_DIST_SERVER:-https://rsproxy.cn}"
export RUSTUP_UPDATE_ROOT="${RUSTUP_UPDATE_ROOT:-https://rsproxy.cn/rustup}"
if [ ! -x "$HOME/.cargo/bin/rustup" ]; then
  curl --proto '=https' --tlsv1.2 --retry 5 -fsSL https://rsproxy.cn/rustup-init.sh |
    sh -s -- -y --profile minimal --default-toolchain stable
fi
source "$HOME/.cargo/env"
export RUSTUP_TOOLCHAIN=stable
rust_version="$(rustc --version 2>/dev/null | cut -d' ' -f2 || true)"
if ! python3 -c 'import sys; v=sys.argv[1].split("."); sys.exit(not (len(v)>=2 and tuple(map(int,v[:2])) >= (1,89)))' "$rust_version"; then
  rustup toolchain install stable --profile minimal
fi
rustup target add aarch64-unknown-linux-gnu

[ -x "$HOME/.local/share/duck-build/bin/python" ] || python3 -m venv "$HOME/.local/share/duck-build"
source "$HOME/.local/share/duck-build/bin/activate"
export PIP_TIMEOUT="${PIP_TIMEOUT:-90}" PIP_RETRIES="${PIP_RETRIES:-8}" PIP_RESUME_RETRIES="${PIP_RESUME_RETRIES:-8}"
python -m pip install --only-binary=:all: \
  -i "${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}" \
  cargo-zigbuild 'ziglang==0.14.1' uv

# Ubuntu 22.04 has 3.10; newer Ubuntu can have 3.14. Leave system Python alone.
if ! python3 -c 'import sys; sys.exit(not (3,11) <= sys.version_info[:2] <= (3,13))'; then
  uv python install 3.12 --no-bin
fi

cargo --version
rustc --version
cargo-zigbuild --version
python -m ziglang version
cargo fetch --locked
