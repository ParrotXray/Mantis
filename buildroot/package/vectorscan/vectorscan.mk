################################################################################
#
# vectorscan
#
################################################################################

# 5.4.11 is the version Ubuntu 24.04 ships as libhyperscan5 on arm64.
VECTORSCAN_VERSION = vectorscan/5.4.11
VECTORSCAN_SITE = https://github.com/VectorCamp/vectorscan.git
VECTORSCAN_SITE_METHOD = git
VECTORSCAN_LICENSE = BSD-3-Clause
VECTORSCAN_LICENSE_FILES = LICENSE
VECTORSCAN_INSTALL_STAGING = YES
VECTORSCAN_SUPPORTS_IN_SOURCE_BUILD = NO
# boost is only needed for its headers, at build time
VECTORSCAN_DEPENDENCIES = boost host-ragel host-pkgconf

# FAT_RUNTIME picks the SSE4.2 / AVX2 / AVX-512 code path at run time,
# like the Ubuntu libhyperscan5 the release binaries were linked against.
# The dispatcher always references the avx2_* variant, which 5.4.11 only
# builds with BUILD_AVX2=ON (default OFF): without it libhs.so.5 has an
# undefined avx2_hs_database_info and Suricata dies at load (exit 127).
VECTORSCAN_CONF_OPTS = \
	-DCMAKE_BUILD_TYPE=Release \
	-DBUILD_SHARED_LIBS=ON \
	-DBUILD_STATIC_LIBS=OFF \
	-DFAT_RUNTIME=ON \
	-DBUILD_AVX2=ON \
	-DBUILD_AVX512=ON \
	-DBUILD_AVX512VBMI=ON \
	-DUSE_CPU_NATIVE=OFF \
	-DBUILD_EXAMPLES=OFF \
	-DBUILD_BENCHMARKS=OFF \
	-DBUILD_CHIMERA=OFF

# Unit tests, tools and docs are not needed on the target and add a lot
# of build time.
define VECTORSCAN_DROP_EXTRAS
	$(SED) '/^add_subdirectory(unit)/d' $(@D)/CMakeLists.txt
	$(SED) '/^add_subdirectory(doc\/dev-reference)/d' $(@D)/CMakeLists.txt
	$(SED) 's/^if (EXISTS .*tools\/CMakeLists.txt)/if (FALSE)/' $(@D)/CMakeLists.txt
endef
VECTORSCAN_POST_PATCH_HOOKS += VECTORSCAN_DROP_EXTRAS

# Every per-CPU variant the dispatcher calls must be in the library.
define VECTORSCAN_CHECK_DISPATCH
	if $(TARGET_NM) -D --undefined-only $(TARGET_DIR)/usr/lib/libhs.so.5 \
		| grep -E ' U (core2|corei7|avx2|avx512|avx512vbmi)_'; then \
		echo "vectorscan: fat runtime variants missing from libhs.so.5" >&2; exit 1; \
	fi
endef
VECTORSCAN_POST_INSTALL_TARGET_HOOKS += VECTORSCAN_CHECK_DISPATCH

$(eval $(cmake-package))
