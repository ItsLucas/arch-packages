#!/usr/bin/env bash
# Trusted host-side launcher. AUR code runs only in the disposable container.
set -euo pipefail
base=${1:?pkgbase}
commit=${2:?commit}
version=${3:?aur_version}
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
provenance=$(python3 - "$script_dir" "$base" "$commit" "$version" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
from pipeline import validate_provenance
row = dict(zip(('pkgbase', 'commit', 'aur_version'), sys.argv[2:]))
validate_provenance(row)
print(json.dumps(row, sort_keys=True))
PY
)
work=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:?TMPDIR required}}/aur-build.XXXXXXXX")
trap 'rm -rf -- "$work"' EXIT
mkdir "$work/recipe"
# Git metadata stays on the trusted runner and is NOT mounted into the container.
git clone --no-checkout "https://aur.archlinux.org/$base.git" "$work/checkout"
git -C "$work/checkout" cat-file -e "$commit^{commit}"
git -C "$work/checkout" archive --format=tar "$commit" | tar -xf - -C "$work/recipe"
python3 - "$work/recipe" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
for path in root.rglob('*'):
    if path.is_symlink() or path.name == '.git':
        raise SystemExit('recipe links/git metadata are not permitted')
PY
# Only explicit, hash-bound audited repairs are applied on the trusted runner.
python3 "$script_dir/recipe_overrides.py" "$base" "$commit" "$work/recipe/PKGBUILD"
# Only the recipe/build directory is mounted. No tokens, git checkout, or socket.
docker run --rm -i \
  --mount "type=bind,src=$work/recipe,dst=/build" \
  -e "BUILDER_UID=$(id -u)" \
  -e COPILOT_AUTO_UPDATE=false \
  -e TMPDIR=/build/.tmp \
  archlinux:base bash -se <<'CONTAINER'
# Root performs official dependency/bootstrap setup; never source PKGBUILD here.
mkdir -p /build/.tmp
pacman -Syu --noconfirm --needed base-devel git sudo
useradd --create-home --uid "$BUILDER_UID" builder
printf 'builder ALL=(root) NOPASSWD: /usr/bin/pacman\n' > /etc/sudoers.d/builder
chmod 0440 /etc/sudoers.d/builder
mkdir -p /build/.tmp /home/builder/.copilot
printf '{"auto_update":false}\n' > /home/builder/.copilot/config.json
chown -R builder:builder /build /home/builder
cd /build
# sudo used by makepkg needs setuid; no-new-privileges would block sudo.
runuser -u builder -- env HOME=/home/builder TMPDIR=/build/.tmp \
  COPILOT_AUTO_UPDATE=false makepkg --syncdeps --noconfirm --cleanbuild
CONTAINER
python3 "$script_dir/pipeline.py" collect "$work/recipe" artifacts "$provenance"
