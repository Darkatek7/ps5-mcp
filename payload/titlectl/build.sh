#!/usr/bin/env bash
# Builds titlectl.elf with the PS5 payload SDK (https://github.com/ps5-payload-dev/sdk), e.g. in WSL:
#
#   PS5_PAYLOAD_SDK=/opt/ps5-payload-sdk payload/titlectl/build.sh   -> payload/titlectl/out/titlectl.elf
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
sdk=${PS5_PAYLOAD_SDK:-/opt/ps5-payload-sdk}
[ -x "$sdk/bin/prospero-clang" ] || { echo "missing $sdk/bin/prospero-clang (set PS5_PAYLOAD_SDK)" >&2; exit 2; }

mkdir -p "$here/out"
"$sdk/bin/prospero-clang" -Wall -Werror -O2 -o "$here/out/titlectl.elf" "$here/titlectl.c" \
    -lSceSystemService -lSceUserService -lSceRegMgr
grep -q "PS5MCP-ARGS-V1:" "$here/out/titlectl.elf" || { echo "the argument slot is missing from the ELF" >&2; exit 1; }
echo "built $here/out/titlectl.elf ($(wc -c < "$here/out/titlectl.elf") bytes)"
