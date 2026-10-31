#!/usr/bin/env bash
set -eo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

[ ! -f "$HOME/.cargo/env" ] || source "$HOME/.cargo/env"
if [ -f "$HOME/.local/share/duck-build/bin/activate" ]; then
  source "$HOME/.local/share/duck-build/bin/activate"
  export CARGO_ZIGBUILD_PYTHON_PATH="$VIRTUAL_ENV/bin/python"
fi

packages=(-p robotd -p mediad -p padd)
for arg in "$@"; do
  case "$arg" in
    -p|--package|--package=*|-p?*|--workspace) packages=() ;;
  esac
done

# mediad and padd must link against the board's GStreamer/udev, not host libraries.
sysroot_env="$(sh scripts/cross-sysroot.sh | grep '^export')"
eval "$sysroot_env"

export CARGO_TARGET_DIR="$PWD/target/native"
export CARGO_PROFILE_RELEASE_INCREMENTAL=true
export CARGO_PROFILE_RELEASE_LTO=off
export CARGO_PROFILE_RELEASE_CODEGEN_UNITS=256

exec cargo board --locked --bins "${packages[@]}" "$@"
