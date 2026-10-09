#!/usr/bin/env python3
"""Audited, commit-scoped repairs; never disable source integrity checks."""
import argparse
import hashlib
from pathlib import Path

I7Z_AUR_COMMIT = '8dfae2ecfcd85ca9d0cdd23d588e2af86a7b18fd'
I7Z_RECIPE_SHA256 = 'fbeb2c9a6e1a8ac87ea8e6ebaa26af6941b2e8a3eff42836dc7757399443e5e9'
I7Z_UPSTREAM_COMMIT = 'ae4191d5102cbee43fce9a85f86f9a9dcd4dab07'
I7Z_OLD_SHA512 = 'b1e13e35df508fdc82f6a2b23aa0389463f9d3f08f6f7fc3d5a563e1ec0cd389271f227d5929ee4a10099b3c1363d4552443614b57970f3a5e64f002aaf029a1'
I7Z_SOURCE_SHA512 = 'c13b9e300a8ddd9fa5a2ebc93745816d218d95cffc2dfd66777d458b865c7d94a3d10c892a89deb6c73b01008dee117dc6131caddf9250c65945fbecf1f0aec1'


def apply(base, commit, path):
    if base != 'i7z' or commit != I7Z_AUR_COMMIT:
        return False
    path = Path(path)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != I7Z_RECIPE_SHA256:
        raise ValueError('reviewed i7z recipe hash mismatch')
    text = data.decode()
    old_source = 'git+https://github.com/afontenot/i7z.git#tag=v${pkgver}'
    if text.count(old_source) != 1 or text.count(I7Z_OLD_SHA512) != 1:
        raise ValueError('reviewed i7z source declaration mismatch')
    text = text.replace(old_source, 'git+https://github.com/afontenot/i7z.git#commit=' + I7Z_UPSTREAM_COMMIT)
    text = text.replace(I7Z_OLD_SHA512, I7Z_SOURCE_SHA512)
    path.write_text(text)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base')
    parser.add_argument('commit')
    parser.add_argument('recipe')
    args = parser.parse_args()
    if apply(args.base, args.commit, args.recipe):
        print('Applied audited i7z integrity repair; source commit and SHA512 are pinned.')


if __name__ == '__main__':
    main()
