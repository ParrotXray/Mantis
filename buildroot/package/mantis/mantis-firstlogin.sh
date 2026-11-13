# /etc/profile.d/mantis-firstlogin.sh: the image ships with an empty root
# password so the first login works on the console; make it the only one.
if [ "$(id -u)" = 0 ] && [ -t 0 ] \
	&& awk -F: '$1 == "root" && $2 == "" { found = 1 } END { exit !found }' /etc/shadow; then
	echo
	echo "  No root password is set. Choose one now: it is needed for SSH, and the"
	echo "  console asks for it from now on. It is kept across upgrades."
	echo
	until passwd root; do
		echo "Try again."
	done
	if mountpoint -q /opt/mantis 2>/dev/null || [ -d /opt/mantis ]; then
		awk -F: '$1 == "root" { print $2 }' /etc/shadow > /opt/mantis/appliance.shadow
		chmod 600 /opt/mantis/appliance.shadow
	fi
	echo
fi
