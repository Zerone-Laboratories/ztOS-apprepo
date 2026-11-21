#!/bin/sh
# Runs inside an Arch Linux ARM container, the base the ztOS image is made from,
# with only the image's packages, so a store app links what a phone has. No secrets.
#   build-in-arch.sh ID VERSION
set -eu
id=$1; version=$2
sed -i 's/^DownloadUser/#DownloadUser/; s/^CheckSpace/#CheckSpace/' /etc/pacman.conf
pacman-key --init >/dev/null
pacman-key --populate archlinuxarm >/dev/null
pacman -Rdd --noconfirm linux-aarch64 >/dev/null 2>&1 || true	# no boot files in a container
grep -v '^#' ci/ztos-packages.txt > /tmp/want
# Mirrors drop files now and then (this failed a 0.1.3 build): try again before giving up.
for try in 1 2 3 4; do
	pacman -Syu --noconfirm --needed - < /tmp/want >/dev/null && break
	[ "$try" = 4 ] && exit 1
	sleep $((try * 30))
done
# build tools only, not linked at run time: Rust apps (v1), CMake and Ninja (v2), wayland-scanner and protocols
pacman -S --noconfirm --needed rust pkgconf cmake ninja wayland wayland-protocols >/dev/null
pacman -Q > out/packages-built-against.txt
repo=$(sed -n 's/^repo = "\(.*\)"/\1/p' sdk.lock)
commit=$(sed -n 's/^commit = "\([0-9a-f]\{40\}\)"/\1/p' sdk.lock)
commit_v2=$(sed -n 's/^commit_v2 = "\([0-9a-f]\{40\}\)"/\1/p' sdk.lock)
fetch() {	# fetch DIR COMMIT: exactly that commit of the SDK
	git init -q "$1"
	git -C "$1" fetch -q --depth 1 "$repo" "$2"
	git -C "$1" checkout -q --detach FETCH_HEAD
	test "$(git -C "$1" rev-parse HEAD)" = "$2"
}
fetch /tmp/sdk "$commit"
make -C /tmp/sdk install PREFIX=/usr >/dev/null	# v1 apps link the system's libzrnui
# v2 apps build zrnui and zrnapp from the source tree (static): nothing installed
case "$commit_v2" in
0000000000000000000000000000000000000000|"") echo "sdk.lock: no SDK v2 pinned yet; SDK v2 apps cannot build" ;;
*) fetch /tmp/sdk2 "$commit_v2" ;;
esac
ZRN_SDK=/tmp/sdk ZRN_SDK2=/tmp/sdk2 python3 tools/store.py build --id "$id" --version "$version" --out out
python3 ci/check-linkage.py out/*.zexec
