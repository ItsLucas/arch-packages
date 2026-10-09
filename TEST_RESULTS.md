# 本地验证记录

## 父任务集成验证（当前进度）

- runner 测试 13 项通过，server 测试 33 项通过，Bash/actionlint 通过；独立审查发现的损坏多-frame zstd 包问题已修复并复审通过，坏包不会签名或切换快照。目标服务器 Python 3.14.4 也实际通过 33 项 server 测试。
- Actions 首次构建 9/10 成功，Copilot 被过严的 recipe symlink 检查拒绝；改为只允许 strict-resolve 后仍在隔离目录内的相对链接，绝对/越界/悬空/环形/.git 链接拒绝，并完成独立审查。
- Actions 第二轮全量构建及发布全部成功：[run 37871675267](https://github.com/ItsLucas/arch-packages/actions/runs/37871675267)，代码 commit `ee0db92e62a4b225da757588f148e3a7fe83ea08`。
- 公网验收实际下载全部 10 个包及 2 个索引、签名与公钥；SHA256、12 份 GPG 签名、DB/files 各 10 条 name/version/filename/hash 全部一致。下载总字节 988461964，逐包记录位于部署工作区 `acceptance-final/VERIFIED.json`。
- 隔离 pacman 在 Required DatabaseRequired 下成功同步，列出全部 10 包；查询实时 AUR 得到空矩阵，不重复构建。系统 pacman.conf、已安装软件未修改，Surface timer 保持 active。
- parent 修复了 USTAR/PAX 传输不兼容与 content-addressed filename 的公开回读不匹配，并加入回归断言。
- i7z 原始配方失败结果保留在下面；经显式审查、commit/hash 约束的完整性修补后，真实 Docker makepkg 已成功构建 `i7z 0.28-1`。证据见 `overrides/README.md`。
- 真实 Docker 构建 `cockpit-file-sharing 4.5.7-1` 与 `cockpit-sensors 1.1-1` 均成功；前者上游 make 输出 system_files 缺失的 ignored error，包构建成功不代表服务端业务功能测试完成。
- 服务端 bootstrap 已用真实 platform-tools 验证 SSH 受限上传、server-only GPG、repo-add、原子快照与公网包/数据库签名和 SHA256；没有修改系统 pacman.conf 或安装包。
- 已完成 10/10 Actions 构建与公开签名仓库验收；详细结果在本节新增记录。以下子任务记录为历史验证过程。

## 子任务最初本地验证（历史结果）

这些结果来自实际本地执行；**尚未执行 GitHub Actions、SSH 远程上传或服务器验收**。
未创建 GitHub 仓库，未 commit/push，未修改服务器或本机 pacman 配置。

## 自动化测试

- TDD：先运行缺失实现的失败测试，再逐步补充 discovery、manifest、产物收集/打包、CLI、workflow 和公开 manifest 回读逻辑。
- `python -m unittest discover -s tests -v`：11 个独立测试全部通过（最后一次 0.186s）。
- `bash -n scripts/build.sh scripts/upload.sh`：通过。
- `actionlint 1.7.12 -shellcheck= .github/workflows/aur.yml`：退出码 0，无输出。
- 覆盖：commit/版本变化与空矩阵；HTTP 404 bootstrap vs 403/429/500 失败；官方 RPC/commit 解析；排除 debug、拒绝符号链接；tar 目录契约与全量/部分门禁；CLI；manifest SHA256 回读；workflow 的 pinned SHA、权限、无构建 secret、部分失败条件；本地 SSH 替身的 stdin、host-key 必须匹配和临时私钥清理。

## 真实官方 AUR 发现

`python3 scripts/pipeline.py discover --output .test-output/live-matrix.json`：退出码 0。
官方 AUR 返回全部 10 个指定 pkgbase 的 40 位提交和 RPC 版本；公开 manifest 缺失，输出
`has_changes=true`、`require_all=true`。

随后把首次发现结果作为**明确标识的本地 HTTP manifest fixture**，再次调用真实官方 AUR
Git/RPC 发现：退出码 0，`has_changes=false`、`matrix={"include":[]}`。
这验证无变化路径，但不声称服务器已经具备这些包。

## 真实 Docker makepkg 构建

### android-sdk-platform-tools：成功

- 发现提交：`cd1fac01e1b42cea764d84e7305726eed6b87558`。
- 官方 RPC / 实际 `.PKGINFO` 版本：`37.0.1-1`，`x86_64`。
- `scripts/build.sh`：退出码 0。
- `artifacts/android-sdk-platform-tools/` 只有 provenance 和
  `android-sdk-platform-tools-37.0.1-1-x86_64.pkg.tar.zst`（7,830,693 字节）。
- makepkg 实际也产生 debug 包，trusted collector 正确排除它。
- 实际 package 成功打包进 `.test-output/platform-tools.tar`；tar 仅含两项
  `<pkgbase>/<filename>`，不会带 `.git` 或 recipe。
- 详细日志：`.test-output/platform-tools-build.log`。

### i7z：上游来源校验失败，未绕过

- 发现提交：`8dfae2ecfcd85ca9d0cdd23d588e2af86a7b18fd`，RPC 版本 `0.28-1`。
- `scripts/build.sh`：退出码 1。
- 官方依赖安装、非 root makepkg 和来源获取正常；验证 sha512sums 时 `i7z ... FAILED`，两项补丁 Passed，随后 `One or more files did not pass the validity check!`。
- 没有使用 `--skipinteg` 或改变 AUR recipe，故没有可上传的 i7z 产物。
- 详细日志：`.test-output/i7z-build.log`。

**部署阻塞项：** 当前 i7z recipe 来源校验失败需要上游修复或用户明确授权、单独审查的解决方案；首次 10/10 验收不能因其余包成功而宣称完成。

## 本地产物位置

代码文件：`.github/workflows/aur.yml`、`scripts/pipeline.py`、`scripts/build.sh`、
`scripts/upload.sh`、`tests/test_pipeline.py`、`tests/test_workflow.py`、`README.md`、`.gitignore`。
日志、下载的 actionlint 工具和本地真实构建产物分别保存在 `.test-output/`、`.tools/`、
`artifacts/`，均被 `.gitignore` 排除；不要把它们提交进私有源码仓库。
