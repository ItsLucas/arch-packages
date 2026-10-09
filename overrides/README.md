# i7z 配方校验修补

仅适用于 AUR commit `8dfae2ecfcd85ca9d0cdd23d588e2af86a7b18fd`；原始 PKGBUILD 保存为 `i7z.original.PKGBUILD`，SHA256 `fbeb2c9a6e1a8ac87ea8e6ebaa26af6941b2e8a3eff42836dc7757399443e5e9`。

上游 `https://github.com/afontenot/i7z` 当前重定向到 `lmfont/i7z`。GitHub tag API 与完整 Git clone 一致确认 `v0.28` 为 commit `ae4191d5102cbee43fce9a85f86f9a9dcd4dab07`，tree `c5696943264d2f4411875f70d0968564c8af3314`，提交时间 2018-04-10T22:15:17Z。

原始 AUR 配方的首个 SHA512 与该提交的 Git archive 不一致。实际运行 Git 2.56.0 和从 Arch 历史包下载、通过 Arch packager 签名验证的 Git 2.52.0，生成的 archive 都是：

```
c13b9e300a8ddd9fa5a2ebc93745816d218d95cffc2dfd66777d458b865c7d94a3d10c892a89deb6c73b01008dee117dc6131caddf9250c65945fbecf1f0aec1
```

没有证据确定 AUR 错误校验值的生成原因；不把它归因于 Git 升级，也不忽略完整性检查。

`recipe_overrides.py` 只在上述 AUR commit + 原始 recipe SHA256 均匹配时，将 tag 改为该固定 upstream commit，并写入其已核对的 SHA512。代码、补丁、编译选项和版本号都保持原样，两个 patch 的原有校验值不变。makepkg 仍执行完整 source 校验，不使用 SKIP 或 --skipinteg。

AUR 后续 commit 不会继承这个修补，默认使用新配方原有校验；如果未来配方仍有错误，构建失败而不是自动更新 checksum。所有本仓库 recipe override 都属于显式本地维护，不声称产物来自未经修改的 AUR 配方。
