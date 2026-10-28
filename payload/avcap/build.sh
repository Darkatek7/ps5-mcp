#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
sdk=${PS5_PAYLOAD_SDK:-/opt/ps5-payload-sdk}
mkdir -p "$here/out"
"$sdk/bin/prospero-clang" -Wall -Wextra -Werror -O2 -o "$here/out/avcap.elf" "$here/avcap.c"
echo "built $here/out/avcap.elf"
