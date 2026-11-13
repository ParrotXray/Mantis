#!/bin/bash
#
# Build the Mantis Buildroot appliance image.
#
#   buildroot/build.sh [make target...]        (default: full build)
#
# Environment:
#   MANTIS_SHA       release build sha (default: .last-built-version, i.e. the latest release)
#   MANTIS_VARIANT   avx512 | avx2 | avx | baseline   (default: avx2)
#   BR_VERSION       Buildroot release (default below, an LTS)
#   BR_SRC           Buildroot source dir (default: buildroot/.br-src, cloned on demand)
#   O                output dir (default: buildroot/output)
#   BR2_DL_DIR       source download cache (Buildroot default: $BR_SRC/dl)
#   BR_CCACHE_DIR    enable ccache with this cache dir (CI)
#
# Result: $O/images/disk.img (write with dd / Etcher), disk.img.gz
# Switching MANTIS_SHA / MANTIS_VARIANT on an existing $O only rebuilds the
# mantis package and the image.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
BR_VERSION="${BR_VERSION:-2026.02.3}"
BR_SRC="${BR_SRC:-$HERE/.br-src}"
O="${O:-$HERE/output}"
MANTIS_SHA="${MANTIS_SHA:-$(cat "$REPO/.last-built-version")}"
MANTIS_VARIANT="${MANTIS_VARIANT:-avx2}"

if [[ ! -d "$BR_SRC" ]]; then
	git clone --depth 1 -b "$BR_VERSION" https://gitlab.com/buildroot.org/buildroot.git "$BR_SRC"
fi

# The app version is part of the asset name; read it from the release.
app_version="${MANTIS_APP_VERSION:-}"
if [[ -z "$app_version" ]]; then
	app_version=$(curl -fsSL "https://api.github.com/repos/ParrotXray/Mantis/releases/tags/build-$MANTIS_SHA" 2>/dev/null \
		| grep -oE '"name": *"mantis-[0-9][^"]*-linux-' | head -n 1 | sed -E 's/.*"mantis-(.*)-linux-$/\1/' || true)
	app_version="${app_version:-0.1.0}"
fi

echo "Mantis build-$MANTIS_SHA ($app_version, $MANTIS_VARIANT), Buildroot $BR_VERSION -> $O"

mantis_release() {
	grep -E '^BR2_PACKAGE_MANTIS_(BUILD_SHA|APP_VERSION|VARIANT)=' "$O/.config" 2>/dev/null || true
}
previous="$(mantis_release)"

make -C "$BR_SRC" O="$O" BR2_EXTERNAL="$HERE" mantis_x86_64_defconfig >/dev/null
config_opts=(
	--set-str BR2_PACKAGE_MANTIS_BUILD_SHA "$MANTIS_SHA"
	--set-str BR2_PACKAGE_MANTIS_APP_VERSION "$app_version"
)
for v in AVX512 AVX2 AVX BASELINE; do
	config_opts+=(--disable "BR2_PACKAGE_MANTIS_VARIANT_$v")
done
config_opts+=(--enable "BR2_PACKAGE_MANTIS_VARIANT_${MANTIS_VARIANT^^}")
if [[ -n "${BR_CCACHE_DIR:-}" ]]; then
	config_opts+=(--enable BR2_CCACHE --set-str BR2_CCACHE_DIR "$BR_CCACHE_DIR")
fi
"$BR_SRC/utils/config" --file "$O/.config" "${config_opts[@]}"
make -C "$BR_SRC" O="$O" olddefconfig >/dev/null

# Buildroot does not notice a changed package version: drop the old release
if [[ -n "$previous" && "$previous" != "$(mantis_release)" ]]; then
	echo "Mantis release changed, rebuilding the mantis package"
	make -C "$BR_SRC" O="$O" mantis-dirclean >/dev/null
fi
make -C "$BR_SRC" O="$O" "$@"
