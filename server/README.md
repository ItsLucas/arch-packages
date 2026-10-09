# 服务端 AUR 接收与签名发布

本目录仅包含接收器、测试和部署说明。**未执行 SSH、部署，也未修改 `repo/` 或 Surface 服务。** 最新方案直接使用 Ubuntu 原生 `repo-add`/`vercmp`；不需要 Docker、sudo wrapper、sudoers 或 aurrepo 的 root 权限。

## 固定运行契约

- 生产入口：`/usr/bin/python3 -I /opt/arch-aur/receive.py`，无参数；`--help` 无副作用。
- 入口拒绝 root，所有发布操作以 `aurrepo` 执行（已分配 uid 991、gid 979）。
- `SSH_ORIGINAL_COMMAND` 只接受空字符串或精确的 `publish`，拒绝 scp、sftp、任意 shell 命令和附加参数。
- 标准输入：**未压缩 USTAR tar**；上传完成后必须关闭 stdin。禁止 PAX/GNU 长路径扩展、链接、设备、FIFO、路径穿越、重复成员、结束标记缺失和非零尾随数据。常规 GNU tar 输出可用，但推荐显式 `--format=ustar`。
- 成员为 `<pkgbase>/provenance.json` 与 `<pkgbase>/<pkgname>-<version-without-epoch>-<arch>.pkg.tar.zst`；允许 `<pkgbase>/` 目录项，**不允许 `./` 前缀**。只上传非空成功构建集；无变更时发布方应跳过 SSH。
- provenance 精确字段：`{"pkgbase":"i7z","commit":"40位小写hex","aur_version":"0.27.2-1"}`。同一 tar 中每个 base 必须具有 provenance 和完整预期包名集合。
- 10 个 base/包名均为：android-sdk-build-tools、android-sdk-cmdline-tools-latest、android-sdk-platform-tools、claude-code、cockpit-file-sharing、cockpit-sensors、github-copilot-cli、google-chrome、i7z、visual-studio-code-bin。默认每个 base 恰好一个同名包；若真实 AUR 引入 split package，需管理员更新 root-owned `ALLOWED`，不能从上传文件扩展白名单。
- `.PKGINFO` 的 pkgbase/pkgname 必须匹配白名单，pkgver 必须等于 provenance 的 AUR 版本，arch 只能为 `x86_64` 或 `any`，上传文件名必须匹配真实元数据。拒绝 debug 包、关键字段重复、未知 PKGINFO key、控制字符，以及非规范 `.PKGINFO` 路径。
- **公开包名为原 stem 加 `.<完整sha256>.pkg.tar.zst`**。上传名仍使用 makepkg 原名；消费者必须从 manifest/DB 读取实际公开 filename，不能自行拼接。这允许“commit 改变但版本相同”的重构，并防止旧 DB 的文件/hash 在切换时被覆盖。pacman 支持 DB 中任意安全 filename。
- 成功 stdout：`{"snapshot":"32位小写hex"}`，退出码 0；失败 stdout 为空，stderr 给出固定拒绝消息，退出码 1。发布端应读取当前 manifest 验证目标 commit，不能只依据 SSH 是否退出成功。

示例（由部署/流水线负责人执行，本次未执行）：

```sh
(cd artifacts && tar --format=ustar -cf - i7z cockpit-sensors) \
  | ssh -T -p 2222 -i dedicated-upload-key aurrepo@2.nb.quq.me
# 也可附加精确的远端命令 publish；必须固定 known_hosts 并启用严格主机校验。
```

## 路径和密钥

```text
/opt/arch-aur/receive.py              root-owned，只读代码
/srv/arch-aur/                       aurrepo-owned，模式 0755
  signing-key                       一行 GPG primary fingerprint，0644
  gnupg/                            aurrepo-owned，0700；绝不进入 public
  .publish.lock                     flock 串行锁
  snapshots/<32hex>/                完整快照；暂存时 0700，提升后 0755
    manifest.json
    repo-key.asc                    仅公钥
    x86_64/<content-addressed-package>[.sig]
    x86_64/lucas-aur.db.tar.gz[.sig]
    x86_64/lucas-aur.files.tar.gz[.sig]
    x86_64/lucas-aur.db[.sig]        相对链接
    x86_64/lucas-aur.files[.sig]     相对链接
  public -> snapshots/<32hex>       原子替换
  repo-key.asc -> public/repo-key.asc
/srv/mirrors/arch/aur -> /srv/arch-aur/public
```

接收器读取 `/srv/arch-aur/signing-key`，不从 SSH 环境、上传文件或 CLI 接收 fingerprint。当前预期 primary fingerprint：

```text
B8D78EC7A67859A6E603B8D04A53C0114627F21D
```

`signing-key` **不是 secret**，可由管理员维护；root owner 可接受，只需 aurrepo 可读。鉴于此环境 `/srv` 为 virtiofs，root 对用户目录无 DAC override，`/srv` 的创建/写入/替换必须实际用 `sudo -u aurrepo` 执行。例如文件尚不存在时：

```sh
sudo -u aurrepo /usr/bin/python3 -c 'from pathlib import Path; p=Path("/srv/arch-aur/signing-key"); p.write_text("B8D78EC7A67859A6E603B8D04A53C0114627F21D\n"); p.chmod(0o644)'
```

若已有 root-owned 配置，按该文件实际权限由管理员维护，**不要重新生成或导出私钥**。接收器验证 GPG `VALIDSIG` 绑定 primary fingerprint，支持该 primary 下合法签名 subkey。

## 原生索引命令

在私有快照 `x86_64` 内，以 aurrepo 执行：

```text
/usr/bin/repo-add --include-sigs --prevent-downgrade lucas-aur.db.tar.gz <manifest中所有latest filenames的显式argv>
```

没有 glob、shell 拼接或 source 上传数据。每个被更新包都先调用 `/usr/bin/vercmp <new> <old>` 拒绝降级；**不能仅依赖 `--prevent-downgrade`，因为新快照重建的是空 DB**。DB 不索引 retained 旧包。索引后核对 DB/files 中每条 name/version/filename/sha256、所有包签名和 hash，再分别签名并验证 DB/files。全部成功后才 `os.replace()` 切换 public。

**安全关键点：** pacman 7.1 的 repo-add 对 `.PKGINFO` 每行执行 Bash `declare "$var=$val"`。仅验证 pkgname/pkgver 不够：恶意 key 可覆写 repo-add 局部变量或通过数组下标执行命令。接收器在调用 repo-add 前对白名单内的所有元数据 key 验证；对应回归测试覆盖 `x[$(touch ...)]`、PATH、tmpdir、pkgfile 注入。

## 部署前提（由父任务处理）

1. Ubuntu universe 安装 `python3`（>=3.11）、`gnupg`、`zstd`、`pacman-package-manager`、`makepkg`、`libarchive-tools`。确认 `/usr/bin/repo-add`、`/usr/bin/vercmp` 存在、makepkg shell libraries 完整；`repo-add --help` 包含上述两个选项。
2. `/opt/arch-aur` 与 `receive.py` 必须 root-owned 且 aurrepo 不可修改；建议 0755 目录、0644 脚本，使用 Python `-I` 忽略 PYTHONPATH/user-site 环境。
3. `/srv/arch-aur` 及 snapshots 归 aurrepo，根目录 0755；GPG home 0700。已存在的 server-only key 应能 batch 无交互签名；接收器不接收密码、不自动开 pinentry。不要把 gnupg 或 `/srv/arch-aur` 整个目录暴露到 Nginx。
4. `/srv` 必须支持 flock、hardlink、目录 fsync、同文件系统原子 symlink replace；目录写入按上述 virtiofs 限制使用 aurrepo 身份。建议独立磁盘 quota（例如 20 GiB），避免多次 SIGKILL 或恶意包耗尽共享磁盘。
5. SSH 专用授权 key 必须由 root 管理，建议 `/etc/ssh/authorized_keys/aurrepo`，不能由 aurrepo 修改。单 key 使用：

   ```text
   restrict,command="/usr/bin/python3 -I /opt/arch-aur/receive.py" ssh-ed25519 <DEDICATED-UPLOAD-PUBLIC-KEY>
   ```

   建议 sshd 对 `Match User aurrepo` 再设置固定 `ForceCommand`、`AuthorizedKeysFile /etc/ssh/authorized_keys/aurrepo`、`AuthenticationMethods publickey`、`PasswordAuthentication no`、`KbdInteractiveAuthentication no`、`PermitTTY no`、`AllowTcpForwarding no`、`AllowAgentForwarding no`、`X11Forwarding no`、`PermitTunnel no`、`PermitUserRC no`。`PermitUserEnvironment` 在本服务器 OpenSSH 的 Match 内不合法；保持全局默认 `no`，并用 `sshd -T -C user=aurrepo,host=2.nb.quq.me,addr=127.0.0.1` 确认有效配置。先 `sshd -t` 校验，避免影响其他账号。

   **注意：OpenSSH 通过账号 shell 的 `-c` 执行 forced command，`/usr/sbin/nologin` 会连接收器一起阻断。** 必须给 aurrepo 可执行的 `/bin/sh`，再通过 root-owned key/sshd `ForceCommand` 禁止真正的交互登录和任意命令；“非登录账号”指没有可用的交互 shell 通道，而不是使用 nologin。账号不得加入 sudo/docker 组，也无需 sudoers。
6. 建立唯一新 mirror symlink `/srv/mirrors/arch/aur -> /srv/arch-aur/public`。**不得修改 `/srv/mirrors/sl7`、`/srv/sl7-apt/public`、已有 Nginx/TLS/端口策略。**

公开 URL：`https://mirrors.5cena.cc/arch/aur/manifest.json`、`.../repo-key.asc`、`.../x86_64/`。保留 shared schema `{"packages":{base:{commit,aur_version,packages:[{name,version,filename,sha256}]}}}`，额外顶层 `retained` 为旧文件记录，发现脚本应只看 `packages`。

## 边界、保留与失败

- 最多 100 条传输成员，总 regular payload <=3 GiB，单 pkg archive <=1 GiB，provenance <=16 KiB，JSON duplicate key 拒绝。
- **签名前完整验证每个包**：保留首个 `.PKGINFO` 的 16 MiB/30 秒扫描上限，但找到元数据后不再提前返回或杀掉 decoder。读取整个解压 tar，验证所有成员 payload、header、两个结束块和零尾随 padding，直至压缩流 EOF；显式要求 zstd 退出码为 0（含后续 frame、checksum 错误）。完整解压数据 <=4 GiB、物理 header <=200000、单次读取 <=1 MiB、验证 wall timeout 300 秒；zstd window <=256 MiB。
- 包内兼容正常 PAX、GNU 长文件名/链接及 makepkg 常用链接。扩展 payload 与累计 PAX metadata 均 <=128 KiB、嵌套扩展深度 <=16；不支持 sparse 扩展，避免恶意映射/稀疏分配。清空 Python tar 成员缓存，流式丢弃 payload，不创建解压临时文件。
- 完整扫描拒绝任何位置的重复 `.PKGINFO`、非规范路径及非 regular 元数据。再用 `/usr/bin/bsdtar -xOf <archive> .PKGINFO`（刻意不加提前退出的 `-q`）完成 libarchive 全 tar 检查，显式检查退出码，并逐字节核对其 metadata 与 Python 结果一致；输出 <=128 KiB。不依赖 repo-add 的 file-list pipeline 传递 bsdtar 错误。
- zstd/bsdtar 验证子进程同样限制每文件 <=32 MiB、地址空间 <=1 GiB、CPU <=180 秒，超时/中断杀整个 process group。不会运行 PKGBUILD、安装脚本或包内程序。
- repo-add 子进程及后代限制：每文件 <=32 MiB、地址空间 <=1 GiB、CPU <=180 秒，整个工具调用 timeout 300 秒；超时或中断杀整个 process group，避免残留子进程。repo-add 临时目录在私有 stage 内，不依赖系统 `/tmp`。
- SSH CLI 总 timeout 1800 秒（包括慢上传和等待 flock）。可在外层用 systemd/cgroup 提供更严格的全进程内存与总磁盘限额；RLIMIT 是每进程/每文件限制，不是 aggregate quota。
- 每个当前快照只保留最新包及一个前一产物（可以是同版本不同 hash）；用 hardlink 复用未变更包，避免指数累积。保留当前、上次 active、另一个 rollback snapshot，共最多 3 个快照；成功切换后清理其余，GC 失败给 warning 不撤销成功发布。
- 验证/签名/索引失败删除 stage 和 intake，current 不变。SIGKILL/断电可能留下 `.intake-*` 或未完成快照；管理员在持有 `.publish.lock` 且确认无发布任务后清理。fsync/atomic replace 为系统承诺边界；提升后磁盘/fsync 错误可能导致客户端看到失败而新 public 已有效，应读回 manifest 后再重试。
- 保留窗口不是无限旧 DB 兼容：连续多次更新后，很久之前的 DB 可能请求已淘汰文件。回滚时持有同一 flock，将 public 原子指向一个已验证的完整 snapshot；不要改文件内容，因为 hardlink 会同时改变其他快照。
- 若 GPG key 要轮换，需协调保留包重新签名；本接收器不会默默接受旧 signer。

## 实际测试

从本目录运行：

```sh
python -m unittest -v test_receive
python -m py_compile receive.py test_receive.py
python -I receive.py --help
```

测试在独立临时状态目录生成临时 GPG key，真实调用 **zstd、bsdtar、GPG、vercmp、原生 repo-add 7.1.0**。只有部分故障/快照测试用独立外部 fake indexer，生产路径另有真实 repo-add 集成测试，并核对签名和 DB 内 `%PGPSIG%`/`%SHA256SUM%`。损坏包回归复现“首个完整 zstd frame 内有合法 PKGINFO，后续 frame 截断”：确认 zstd/bsdtar 全检查失败而 repo-add 使用的快速 metadata 提取成功，断言不调用签名、不切换 public 且清理 stage。另覆盖完整 tar 后 decoder checksum 错误、较晚重复/非规范 PKGINFO、PAX `path=.PKGINFO/` 导致的 Python/libarchive metadata 分歧、损坏 tar payload/header/尾随数据、扩展分配限制与流输出/timeout 上限。正例通过原生 repo-add 发布多 frame、17 MiB payload、PAX/xattr、GNU 长路径与长 symlink 包。还覆盖 unsafe transport、identity/注入、metadata bomb、降级、commit-only 重构、保留窗口、公开权限、并发 flock，以及 timeout 后代清理。无网络、不安装包、不修改本机 pacman 源、不使用生产密钥。
