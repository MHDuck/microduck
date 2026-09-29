#!/usr/bin/env bash
set -euo pipefail

sudo apt-get update
sudo apt-get install -y build-essential pkg-config curl ca-certificates python3-venv

export RUSTUP_DIST_SERVER=https://rsproxy.cn
export RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup
if [ ! -x "$HOME/.cargo/bin/rustup" ]; then
  curl --proto '=https' --tlsv1.2 -fsSL https://rsproxy.cn/rustup-init.sh |
    sh -s -- -y --profile minimal --default-toolchain stable
fi
source "$HOME/.cargo/env"
rustup target add aarch64-unknown-linux-gnu

python3 -m venv "$HOME/.local/share/duck-build"
source "$HOME/.local/share/duck-build/bin/activate"
python -m pip install --only-binary=:all: \
  -i https://pypi.tuna.tsinghua.edu.cn/simple \
  cargo-zigbuild 'ziglang==0.14.1'

cargo --version
rustc --version
cargo-zigbuild --version
python -m ziglang version
