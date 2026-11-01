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
pacman -S --noconfirm --needed rust pkgconf >/dev/null	# builds Rust apps; not linked at run time
pacman -Q > out/packages-built-against.txt
repo=$(sed -n 's/^repo = "\(.*\)"/\1/p' sdk.lock)
commit=$(sed -n 's/^commit = "\([0-9a-f]\{40\}\)"/\1/p' sdk.lock)
git init -q /tmp/sdk
git -C /tmp/sdk fetch -q --depth 1 "$repo" "$commit"
git -C /tmp/sdk checkout -q --detach FETCH_HEAD
test "$(git -C /tmp/sdk rev-parse HEAD)" = "$commit"
make -C /tmp/sdk install PREFIX=/usr >/dev/null
ZRN_SDK=/tmp/sdk python3 tools/store.py build --id "$id" --version "$version" --out out
python3 ci/check-linkage.py out/*.zexec
