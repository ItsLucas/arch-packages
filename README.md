# lucas-aur：每日 AUR 二进制镜像

私有源码仓库：`ItsLucas/arch-packages`。公开镜像：
`https://mirrors.5cena.cc/arch/aur/x86_64/`；数据库名 `lucas-aur`。

## 工作方式

每日 UTC 20:23（北京时间次日 04:23）及手动触发。每次从官方 AUR Git
解析 `master` 的完整提交，再读取 AUR RPC 版本；提交或版本变化才构建。
网络错误、403/429/5xx、错误 JSON 都会使发现任务失败；只有 manifest 404
表示首次部署。没有变化不会运行构建或上传。手动勾选 `force` 会重建全部包。

跟踪 10 个 pkgbase：

- android-sdk-build-tools
- android-sdk-cmdline-tools-latest
- android-sdk-platform-tools
- claude-code
- cockpit-file-sharing
- cockpit-sensors
- github-copilot-cli
- google-chrome
- i7z
- visual-studio-code-bin

构建矩阵在独立 Ubuntu runner 上启动 `archlinux:base` Docker 容器。
可信 runner 的 Git 只导出发现时固定提交的 recipe；只挂载这个临时构建目录，
不挂载仓库、`.git`、Docker socket，也不传入 GitHub/上传凭据。
容器先从 Arch 官方仓库安装 `base-devel` 和依赖工具，随后以 `builder`
执行 `makepkg --syncdeps --noconfirm --cleanbuild`。不递归构建任意 AUR 依赖，
不绕过来源校验。容器中 sudo 仅允许运行 pacman；Docker 是隔离边界，不是
防恶意内核攻击的虚拟机。必须信任/审查 AUR recipe，Git 固定提交并不证明其安全。

上传 job 才在 SSH 步骤读取专用 key。Action 均固定为从 GitHub API 解析的
完整 commit SHA，checkout 设置 `persist-credentials: false`，权限仅
`contents: read`。整个流水线串行发布，不取消正在运行的构建。
已有镜像的日常更新允许部分构建失败：只上传成功的 pkgbase，服务器保留其他旧包；
流水线仍显示失败，以便检查失败包。首次部署/强制全量必须收齐全部矩阵产物才上传，
不得将部分成功称为首次部署完成。

## 配置（由部署者完成）

创建**私有**仓库后设置：

| 类型 | 名称 | 内容 |
| --- | --- | --- |
| Secret | `AUR_UPLOAD_KEY` | 专用受限 SSH 私钥，不得复用管理 key |
| Variable | `AUR_UPLOAD_HOST` | `2.nb.quq.me` |
| Variable | `AUR_UPLOAD_PORT` | `2222` |
| Variable | `AUR_UPLOAD_USER` | `aurrepo` |
| Variable | `AUR_SSH_KNOWN_HOSTS` | 经可信渠道确认的完整 `[2.nb.quq.me]:2222 ssh-ed25519 …` host key 行 |

不要运行时 `ssh-keyscan` 后直接信任，也不要关闭 host-key 检查。
服务器必须预先配置 `restrict`/forced-command intake（不可交互登录），只读取 stdin tar；
GPG 签名 key 仅保留在服务器，CI 无需也不应持有它。服务端实现与部署说明见 `server/`。

每个 artifact 内容为 `<pkgbase>/provenance.json` 和非 debug 的 `*.pkg.tar.zst`：

```json
{"pkgbase":"github-copilot-cli","commit":"<40位小写十六进制AUR提交>","aur_version":"<官方RPC版本>"}
```

下载时 `merge-multiple: true` 保留不同 pkgbase 目录。发布前校验 provenance
完全匹配本次 discovery，拒绝符号链接、额外文件和 debug 包，然后生成
`<pkgbase>/<filename>` tar。服务器还须独立校验 `.PKGINFO`、来源版本、文件哈希、
大小及安全路径，签名后生成 repo-add 数据库并原子切换快照。SSH 成功后 CI
读取公开 manifest，对每个上传 pkgbase 的 commit、RPC version 和全部包 SHA256
逐项核验；这不替代首次部署的完整 GPG/数据库/pacman 验收。

公开 manifest：`https://mirrors.5cena.cc/arch/aur/manifest.json`。
公开 filename 带完整 SHA256，CI 按相同内容寻址规则核对；不要用原始 makepkg 文件名猜下载地址。
字段：`packages[base] = {commit, aur_version, packages:[{name,version,filename,sha256}]}`。

### 显式配方修补

`i7z` 的一个 AUR commit 存在错误 source SHA512。仅对已核对的 recipe hash，将 source 固定到同一上游 v0.28 的完整 commit，并修正为实测 SHA512，不改代码或版本、不跳过校验。范围与验证证据见 [overrides/README.md](overrides/README.md)。AUR 后续 commit 不自动套用此修补。

## 客户端配置（仅说明，不会自动修改本机）

先获取公钥，**通过另外的可信渠道核对部署者公布的完整指纹**，确认后再导入：

```bash
curl -fLo repo-key.asc https://mirrors.5cena.cc/arch/aur/repo-key.asc
gpg --show-keys --with-fingerprint repo-key.asc
sudo pacman-key --add repo-key.asc
# 用核对后的真实完整指纹替换下方占位符，不要原样执行。
sudo pacman-key --lsign-key '<已核对的完整指纹>'
```

再手动添加至 `/etc/pacman.conf`，不要改动已有 Surface 源：

```ini
[lucas-aur]
SigLevel = Required DatabaseRequired
Server = https://mirrors.5cena.cc/arch/aur/$arch
```

应用自行下载新版本会绕开 pacman 和镜像签名管理。CI 设置
`COPILOT_AUTO_UPDATE=false`（见 [官方 CLI 环境变量参考](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference)），
并在容器内写入 `~/.copilot/config.json` 的 `auto_update: false`。
本机用户应按自己的部署方式显式设置同一环境变量，或在自己的
`~/.copilot/config.json` 中合并 `"auto_update": false`，不要覆盖已有其他配置。
其他应用（如 VS Code）也应检查自身更新策略；这里**不会修改任何本机配置**。

## 许可证与责任

私有源码仓库不意味着构建产物可以公开再分发。公开镜像必须逐一满足各软件的
许可证、EULA、版权声明、源码提供义务及商标限制，尤其 Google/Android、Chrome、
VS Code、Claude 和 Copilot 等专有组件。上线前应核查最新条款；不能再分发的包
不得仅因 AUR 有 PKGBUILD 就公开提供。签名只证明仓库发布者，不是上游安全保证。

## 本地测试

只需 Python 3.11+、Bash 和 OpenSSH 的 `ssh-keygen`（测试用本地替身接收 SSH stdin，不连接远程）：

```bash
python3 -m unittest discover -s tests -v
bash -n scripts/build.sh scripts/upload.sh
# 可选：独立验证 GitHub Actions 语法
# actionlint -shellcheck= .github/workflows/aur.yml
```

Workflow 文件使用 JSON 形式的 YAML，GitHub Actions 支持该格式；这样 unittest
可以仅用 stdlib 精确验证结构。`actionlint` 验证不等于在 GitHub 实际运行过 Actions。
本地实际构建需 Docker 和联网，使用专门临时目录，例如：

```bash
mkdir -p .test-output
python3 scripts/pipeline.py discover --output .test-output/matrix.json
# 从 matrix.json 取真实 pkgbase/commit/aur_version 后：
RUNNER_TEMP="$PWD/.test-output" bash scripts/build.sh '<pkgbase>' '<commit>' '<aur_version>'
```

当前 AUR recipe 可能发生下载/校验/依赖失败；应修复上游或审查专门的变更，
不要使用 `--skipinteg` 等绕过校验。首次全量运行必须 10/10 成功，另需验证公开
数据库、包签名、公钥和隔离 pacman 同步，才可宣布部署完成。
