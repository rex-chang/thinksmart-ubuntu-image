# 在线更新（thinksmart-update）：语音客户端和系统层

只检查不自动装，用户在屏幕上点「更新」才装；只从 HTTPS 地址下载，每个文件按清单核对大小和 sha256；保留上一版；装完自检，不过就自动退回；用户数据不碰。

## 组成

| 文件 | 装到设备哪里 | 作用 |
| --- | --- | --- |
| `thinksmart-update` | `/usr/local/sbin/` | root 运行：`check` 检查、`run` 处理页面请求、`install` 安装、`boot` 断电恢复、`adopt` 把现有客户端目录收编为一个版本 |
| `thinksmart-update.conf` | `/etc/thinksmart-update.conf` | 更新地址（可多个，依次试，必须 https；本机 127.0.0.1/localhost 的测试源可以 http）、代理 |
| `thinksmart-update-check.timer/.service` | `/etc/systemd/system/` | 开机 2 分钟后和每天一次检查（只检查） |
| `thinksmart-update-request.path` + `-run.service` | 同上 | 页面放进 `/var/lib/thinksmart-update/requests/` 的请求由 root 服务处理；最多跑 3 小时（国内直连 GitHub 慢），超时被杀或崩溃时 `OnFailure` 立刻跑一次检查，把停在「正在更新」的状态收尾 |
| `thinksmart-update-boot.service` | 同上 | 开机时把「切换后还没自检完」的更新重新自检或退回 |

`check`、`run`、`install` 拿到锁后也先做一遍 `boot` 的收尾：能拿到锁说明上一个进程已经不在了，还停在准备/下载/安装/自检/退回的任务都是被中断的，切换后没自检完的补测或退回，其余标成失败，页面的更新按钮重新出现。
| `install.sh` | — | 在设备上 `sudo sh install.sh` 装好以上全部，并把 `~/thinksmart-client` 收编为当前版本（系统层的其余部分用 `image/device-setup.sh files`） |
| `make-release.py` | Mac 上用 | 打包客户端和系统层、建依赖环境（arm64 容器）、写清单（版本、更新内容、每个文件的大小和 sha256）和 Release 说明 |

设备上的目录：`/opt/thinksmart/client/<版本>/`（root 所有）、`current`/`previous` 链接、`/opt/thinksmart/envs/<依赖哈希>/`；状态 `/var/lib/thinksmart-update/status.json`（所有人可读，客户端和欢迎页读它显示）。

## 系统层

系统层 = 本仓库装到设备上的文件（展示屏外壳、欢迎页、键盘、熄屏和接近传感器、麦克风服务、音频和蓝牙配置、更新服务自己）。

- **装哪些、改了重启什么**：`image/system-files.txt` 一行一个文件（源、目标、权限、属主、改了要做什么），`image/system-post.sh` 做剩下的杂事（服务开机启动、BlueZ 设置）。`image/device-setup.sh files` 和在线更新用的是同一张表。只允许装到 `/usr/local/`、`/etc/`、`/home/ubuntu/.config/` 下；标 `keep` 的是用户配置（`/etc/thinksmart-kiosk.conf`、`/etc/thinksmart-update.conf`），只在没有时装，绝不覆盖。内核、boot 分区、apt 包不在表里，要重刷。`require 包名` 写系统层需要的 apt 包，设备上没有就提示要重刷，不装。
- **版本**：本仓库根目录 `VERSION`，和客户端互不绑定。设备上记在 `/opt/thinksmart/system/installed.json`，没有这个文件就是 0.1.0（20261101 镜像）。
- **安装**：下载、核对大小和 sha256 → 解包、核对文件集合的 id（清单里的 `tree`）→ 把表里所有目标文件当前的样子备份到 `/opt/thinksmart/system/backup/`（不存在的也记下）→ 装内容变了的文件、跑收尾脚本 → 只重启用到变了的文件的服务：外壳 → 重启展示屏；麦克风 → 重启 thinksmart-mic 和客户端；WirePlumber/PipeWire 配置 → 先停 thinksmart-mic 再重启 WirePlumber（镜像 README 3.4），记下并恢复音量；BlueZ → 重启 bluetooth；只在开机读的（zram、NetworkManager）→ 提示「重启设备后生效」。
- **自检**（90 秒）：重启过的服务都在运行且 10 秒内没再重启；外壳要看到浏览器进程稳定；动过音频要客户端 `/api/health` 的麦克风、扬声器正常；换过更新服务要新脚本能跑 `selftest`。
- **退回**：自检没过、出错、或者装到一半断电（开机时 `boot` 发现 `system-pending.json`），就把备份逐个放回（新加的文件删掉、新加的单元先 disable），再重启一遍、复查。
- **和客户端一起**：状态文件里客户端字段照旧在顶层，系统层在 `system` 下；页面一个请求带两个版本号，先装系统层，系统层没装成就停下，客户端这次不装。
- **20261101 镜像的设备**：上面的更新服务只认客户端。客户端包里带一份本仓库的 `thinksmart-update`（`make-release.py --system` 放进 `deploy/updater/`），客户端服务启动前以 root 跑 `deploy/bootstrap-updater`，发现设备上的更新服务比它旧就换上。所以老设备先装一次客户端更新，就能看到系统层更新了。
- `thinksmart-update system-verify`：打印系统层版本和文件集合 id，有文件被手改过就返回 1。`pull-from-device.sh` 用它，`build-image.sh` 拿它和最新的发布比对。

## 发一个版本

```bash
# 客户端改了：改 thinksmart_client/__init__.py 的 __version__；系统层改了：本仓库 VERSION 加一。都提交，然后
python3 make-release.py ../../xiaozhi-thinksmart-client /tmp/release --system .. \
    --note "客户端改了什么" --system-note "系统层改了什么"
```

- 产物：`latest.json`、客户端包、依赖环境包、系统层包，和一个 `release-<标签>.txt`（标签、标题、要上传的文件、可以直接粘贴的说明）。在**客户端仓库**建这个标签的 Release、勾选 Latest、上传这几个文件。标签 `c<客户端版本>-s<系统层版本>`，比如 `c0.1.4-s0.2.0`。
- 系统层包永远不发到本仓库的 Releases：本仓库的 Releases 只放刷机镜像，免得刷机的人不知道下哪个。
- 系统层版本和文件绑定：第一次发 `VERSION` 时在本仓库打本地标签 `system-v<版本>`；以后 `VERSION` 没变但系统层文件变了，脚本会报错，要求先加版本号。系统层文件没提交也会报错。
- 只改了客户端时，系统层版本不变，设备不会提示系统层；只改了系统层时，客户端版本不变，设备只提示系统层。
- **打新镜像**：先发在线更新、在设备上装好，再 `pull-from-device.sh` 和 `RELEASE_MANIFEST=<latest.json 的文件或地址> build-image.sh …`。镜像里的客户端版本、系统层版本和文件集合必须和这次发布一模一样，否则 `build-image.sh` 报错停下，这样新刷的设备检查更新时显示「已是最新」。测试镜像可以用 `ALLOW_UNRELEASED=1` 跳过。

2026-11-01 起不签名、不用密钥（用户定）：设备只从 HTTPS 地址取清单和文件，每个文件都要和清单里的大小、sha256 对上才装，所以下坏的、没下完的包不会装上。代价：谁能改 Release 上的文件（拿到 GitHub 账号）或冒充 HTTPS 服务器，谁就能给设备推包。

产物按 GitHub Releases 的路径排好：把 `latest.json`、代码包、依赖包三个文件上传到标签为 `client-vX.Y.Z` 的 Release（每个组件一个仓库，`releases/latest` 才指得对）。上传由人来做。

## 测试

本机起一个静态文件服务指向 `build` 的输出目录，经 SSH 反向隧道给设备用，设备上 `/etc/thinksmart-update.conf` 的 `SOURCES` 改成 `http://127.0.0.1:<端口>/`。单元测试：`uv run --no-project --with pytest python -m pytest -q update`。步骤和结果见交付文档。

更新没有签名（2026-11-01 定）：设备只认 HTTPS 和清单里每个文件的 sha256，更新地址上放什么就会以 root 身份装什么。`SOURCES` 只填自己的或信任的地址，不要填公共的 GitHub 加速镜像或代理站。
