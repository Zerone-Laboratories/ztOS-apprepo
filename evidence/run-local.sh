#!/bin/sh
# The whole store pipeline on one machine, with no GitHub and no Discord:
# plan, build the sample at its pinned commit (from a local clone of zrn-sdk
# standing in for GitHub), publish with a throwaway test key, index, and check
# it all as a client would. Writes evidence/local-run/.
#   ZRN_SDK=../zrn-sdk sh evidence/run-local.sh
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
sdk=${ZRN_SDK:-$here/../zrn-sdk}
export ZRN_SDK=$sdk
work=$(mktemp -d)
out=$here/evidence/local-run
mkdir -p "$out"
export ZRN_CONFIG_HOME=$work/cfg XDG_CACHE_HOME=$work/cache
store="python3 $here/tools/store.py --staging"
{
	echo "# $(date -u +%FT%TZ) on $(uname -m)"
	echo "\$ zrn keygen (a throwaway test store key)"
	python3 "$sdk/tools/zrn" keygen --output "$work/store.key" --no-trust
	echo "\$ store.py validate";  python3 "$here/tools/store.py" validate
	echo "\$ store.py plan";      $store plan --index-tree "$work/tree"
	echo "\$ store.py build (at the pinned SHA, twice: the same bytes)"
	$store build --id zrn-example-hello --version 0.1.0 --out "$work/artifacts" \
		--source-override "https://github.com/Zerone-Laboratories/zrn-sdk=$sdk"
	$store build --id zrn-example-hello --version 0.1.0 --out "$work/again" \
		--source-override "https://github.com/Zerone-Laboratories/zrn-sdk=$sdk"
	sha256sum "$work"/artifacts/*.zexec "$work"/again/*.zexec | sed "s|$work/||"
	echo "\$ store.py publish (x86_64 allowed: this laptop has no aarch64 compiler)"
	$store publish --artifacts "$work/artifacts" --index-tree "$work/tree" --assets-out "$work/assets" \
		--key "$work/store.key" --allow-arch
	echo "\$ store.py index";     $store index --index-tree "$work/tree" --key "$work/store.key" --base-url "file://$work/assets"
	echo "\$ store.py check-index (as a client, with the store key)"
	python3 "$here/tools/store.py" check-index --index "$work/tree/index.json" --pubkey "$work/store.pub"
	echo "\$ check-index with another key (must refuse)"
	python3 "$sdk/tools/zrn" keygen --output "$work/other.key" --no-trust >/dev/null
	python3 "$here/tools/store.py" check-index --index "$work/tree/index.json" --pubkey "$work/other.pub" || echo "(refused, exit $?)"
	echo "\$ plan again (nothing left to build)"; $store plan --index-tree "$work/tree"
} > "$out/log.txt" 2>&1
cp "$work/tree/index.json" "$work/tree/index.json.minisig" "$out/"
cp "$work/store.pub" "$out/test-store-key.pub"
cp -r "$work/tree/releases" "$out/"
cp "$work"/assets/*/*.zexec "$work"/assets/*/*.minisig "$out/"
rm -rf "$work"
echo "wrote $out"
