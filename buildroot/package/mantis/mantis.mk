################################################################################
#
# mantis
#
################################################################################

MANTIS_VERSION = $(call qstrip,$(BR2_PACKAGE_MANTIS_BUILD_SHA))
MANTIS_APP_VERSION = $(call qstrip,$(BR2_PACKAGE_MANTIS_APP_VERSION))
MANTIS_VARIANT = $(call qstrip,$(BR2_PACKAGE_MANTIS_VARIANT))
MANTIS_SITE = https://github.com/ParrotXray/Mantis/releases/download/build-$(MANTIS_VERSION)
MANTIS_SOURCE = mantis-$(MANTIS_APP_VERSION)-$(MANTIS_VARIANT)-$(MANTIS_VERSION).tar.gz
MANTIS_LICENSE = GPL-3.0
MANTIS_DIST_DIR = /usr/share/mantis-dist

MANTIS_DEPENDENCIES = \
	elfutils zlib libevent hiredis libmaxminddb file libunwind libcap-ng \
	libnet libnetfilter_queue libnfnetlink jansson libyaml pcre2 libpcap \
	lz4 vectorscan

# The release is built on Ubuntu 24.04, whose sonames differ from the
# upstream builds for three libraries; point them at what Buildroot builds
# (same API, the embedded Suricata uses no versioned symbols from them).
# check-needed.py fails the build if anything else is unresolved.
MANTIS_SONAME_LINKS = \
	libpcap.so.0.8:libpcap.so \
	libnet.so.1:libnet.so \
	libhiredis.so.1.1.0:libhiredis.so

define MANTIS_INSTALL_TARGET_CMDS
	rm -rf $(TARGET_DIR)$(MANTIS_DIST_DIR)
	mkdir -p $(TARGET_DIR)$(MANTIS_DIST_DIR)
	cp -a $(@D)/. $(TARGET_DIR)$(MANTIS_DIST_DIR)/
	echo "$(MANTIS_VERSION) $(MANTIS_VARIANT)" > $(TARGET_DIR)$(MANTIS_DIST_DIR)/.dist-version
	for pair in $(MANTIS_SONAME_LINKS); do \
		link=$${pair%%:*}; dev=$${pair##*:}; \
		real=$$(basename $$(readlink -f $(STAGING_DIR)/usr/lib/$$dev)); \
		test -e $(TARGET_DIR)/usr/lib/$$real || exit 1; \
		ln -sf $$real $(TARGET_DIR)/usr/lib/$$link; \
	done
	$(INSTALL) -D -m 0755 $(MANTIS_PKGDIR)/mantis-setup \
		$(TARGET_DIR)/usr/sbin/mantis-setup
	$(INSTALL) -D -m 0644 $(MANTIS_PKGDIR)/bypass.sh \
		$(TARGET_DIR)/usr/lib/mantis/bypass.sh
	$(INSTALL) -D -m 0644 $(MANTIS_PKGDIR)/slots.sh \
		$(TARGET_DIR)/usr/lib/mantis/slots.sh
	$(INSTALL) -D -m 0644 $(MANTIS_PKGDIR)/xsk.sh \
		$(TARGET_DIR)/usr/lib/mantis/xsk.sh
	$(INSTALL) -D -m 0755 $(MANTIS_PKGDIR)/mantis-upgrade \
		$(TARGET_DIR)/usr/sbin/mantis-upgrade
	$(INSTALL) -D -m 0755 $(MANTIS_PKGDIR)/mantis-logrotate \
		$(TARGET_DIR)/usr/sbin/mantis-logrotate
	$(INSTALL) -D -m 0755 $(MANTIS_PKGDIR)/mantis-ntp-event \
		$(TARGET_DIR)/usr/sbin/mantis-ntp-event
	$(INSTALL) -D -m 0644 $(MANTIS_PKGDIR)/mantis-firstlogin.sh \
		$(TARGET_DIR)/etc/profile.d/mantis-firstlogin.sh
	$(INSTALL) -D -m 0644 $(MANTIS_PKGDIR)/acpid.conf \
		$(TARGET_DIR)/etc/acpid.conf
	$(INSTALL) -D -m 0755 $(MANTIS_PKGDIR)/acpi-power-button \
		$(TARGET_DIR)/etc/acpi/PWRF/00000080
endef

define MANTIS_INSTALL_INIT_SYSV
	$(INSTALL) -D -m 0755 $(MANTIS_PKGDIR)/S05trialdog \
		$(TARGET_DIR)/etc/init.d/S05trialdog
	$(INSTALL) -D -m 0755 $(MANTIS_PKGDIR)/S10acpid \
		$(TARGET_DIR)/etc/init.d/S10acpid
	$(INSTALL) -D -m 0755 $(MANTIS_PKGDIR)/S20data \
		$(TARGET_DIR)/etc/init.d/S20data
	$(INSTALL) -D -m 0755 $(MANTIS_PKGDIR)/S21bypass \
		$(TARGET_DIR)/etc/init.d/S21bypass
	$(INSTALL) -D -m 0755 $(MANTIS_PKGDIR)/S22security \
		$(TARGET_DIR)/etc/init.d/S22security
	$(INSTALL) -D -m 0755 $(MANTIS_PKGDIR)/S42ntp \
		$(TARGET_DIR)/etc/init.d/S42ntp
	$(INSTALL) -D -m 0755 $(MANTIS_PKGDIR)/S41mgmt-net \
		$(TARGET_DIR)/etc/init.d/S41mgmt-net
	$(INSTALL) -D -m 0755 $(MANTIS_PKGDIR)/S90mantis \
		$(TARGET_DIR)/etc/init.d/S90mantis
	$(INSTALL) -D -m 0755 $(MANTIS_PKGDIR)/S99upgrade \
		$(TARGET_DIR)/etc/init.d/S99upgrade
endef

$(eval $(generic-package))
