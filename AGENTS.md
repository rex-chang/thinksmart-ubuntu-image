# AGENTS.md — 给 ZCode/Agent 的工作指引

Lenovo ThinkSmart View（CD-18781Y）改装机：Ubuntu 24.04 + postmarketOS 内核的**系统层仓库**（kiosk 壳、熄屏、音频、更新器），外加从 fubin0478/thinksmart-view 合并而来的应用层（主界面、网易云音乐、AirPlay、USB 工具）。

**先读 `README.md` 的「给 AI 助手的说明（先读这一节）」**——EDL 刷机、写分区、刷 lk2nd/bootstrap 属于危险操作，必须先得到用户明确同意；仓库对"能自动做"与"必须问"有明确清单。

## 目录速览

| 目录 | 内容 |
| --- | --- |
| `image/` | 出镜像/抓取/还原；`system-files.txt` 是**唯一**的系统层文件安装表 |
| `kiosk/` | Weston + WebKitGTK 全屏壳、欢迎页、屏幕键盘、GPU 看门狗 |
| `home/` | 主界面服务（8095：天气/日历/音乐卡/番茄钟/留言板）、亮度面板、`home_overlay.js`；`deploy.sh` |
| `screen/` | 熄屏守护（夜间/日出/WebKit 冻结）、VCNL4200 光感/接近、加速度唤醒 |
| `netease/` | 网易云：`hub/`（hub 侧插件 + 打补丁脚本）、`app/`（8091 触屏 App）、`tests/`、`deploy.sh` |
| `intercom/`、`airplay/`、`usb/` | 对讲（未启用，摄像头无驱动）、AirPlay、USB 工具 |
| `xiaozhi/`、`bluetooth/`、`zram/`、`mesa/`、`codec-dmic/`、`kernel/`、`update/` | 麦克风/音频配置、蓝牙、压缩内存、Mesa 补丁、内核、OTA 更新器 |

## 测试与静态检查

```sh
uv run --no-project --with pytest python -m pytest -q screen           # 熄屏守护（18）
uv run --no-project --with pytest python -m pytest -q kiosk            # kiosk（5）
PYTHONPATH=netease/hub uv run --no-project --with pytest --with requests \
  --with aiohttp --with cryptography python -m pytest -q netease/tests/   # 网易云（56）
python3 -m py_compile <改动的 .py>
bash -n <改动的 .sh>
```
- uv 拉依赖慢/失败时加 `UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`（同理 pip 用 `PIP_INDEX_URL`）。
- netease 测试需要 hub 在 `PYTHONPATH`（`plugins_func` 包根），且带 requests/aiohttp/cryptography。

## 部署（设备 10.9.1.60，用户 ubuntu）

- 本机没有 `~/.ssh/thinksmart_ed25519`；`ts.sh` 不可用，直接 `ssh -o BatchMode=yes ubuntu@10.9.1.60`（默认密钥已授权）。
- hub 侧：`./netease/deploy.sh`（打补丁 + 重启 hub + 自检）。
- 应用/系统层：`PIP_INDEX_URL=… ./home/deploy.sh`（经设备上的 `thinksmart-update system-apply` 安装，可选 `--airplay`/`--intercom`）。
- **部署脚本必须在 macOS 自带 bash 3.2 下可跑**：空数组展开用 `${ARR[@]+"${ARR[@]}"}` 惯用法。

## 架构边界与改动规则

- **`image/system-files.txt` 是权威安装表**：任何要落到设备的文件必须加行（源 → 目的地 → 权限 → 重启动作）；新增顶层目录还要同步 `update/make-release.py` 的 `SYSTEM_DIRS` 和 `home/deploy.sh` 的 scp staging 列表，否则 system-apply 报"系统层文件表有一行不对"。
- 网易云 hub 补丁（`netease/hub/patch/*.py`）是**版本块 + V2_FIXES 文本替换**机制：给已打补丁的设备修 bug 必须加 fix 条目（块级标记会 `already` 跳过）；fix 的锚点必须**覆盖整个函数头**——裸 `read(1025)` 这类行在登录补丁的 `_netease_body` 里也有一份，`replace(...,1)` 会改错函数（真实事故）。程序化生成 pair 文本时用真实换行 + `repr()` 输出。
- 被 overlay 框进来的子页面（8091 等）必须自己 postMessage `{ts:'sub', cmd:'ready'}` 给父窗口，否则 kiosk 显示"页面暂时无法打开"。
- 网易云播放：eapi 指纹被风控（-460，与 IP/会话无关），`api.song_url` 已带网页版接口回退；网页接口对无版权歌返回 `data[].code 404` → LookupError → 播放器跳过，属正常。不要"修复"为删除回退。
- 主界面音乐卡 / 语音点歌 / 8091 App 共享 hub 侧同一个 mpv 队列；改播放逻辑时三处一起想。

## 设备排查 gotcha

- hub stdout 进 /dev/null，真实日志在 `<xiaozhi-server>/tmp/server.log`；hub 进程是 `.venv/bin/python app.py`（用 `pgrep -f app.py`，`ps -C python3` 查不到）；`journalctl -M ubuntu@` 静默返回空，直接以 ubuntu 身份 `journalctl --user`。
- `system-apply`/deploy 报告"user 单元已重启"不可信——核对进程 `lstart`，stale 就手动 restart（kiosk、idle-blanker、音乐 App 都发生过）。
- `/dev/fb0` 截图全黑（Weston 走 DRM 不走 fbdev）；远程唤醒熄屏 = 改 `/run/user/1000/thinksmart-screen.json` 的 `seq`。
- 设备 python 是 3.12，无桌面依赖；本机是 macOS bash 3.2，**不要**在脚本里用 bash 4+ 特性。
- 在设备上跑多行 Python 探针：scp 脚本文件过去再执行，不要在单引号 ssh 命令里内嵌含引号的 heredoc。

## 测试样例与文档

- `docs/superpowers/` 有历史设计/计划文档；改动敏感区（更新器、镜像构建）前先读对应 `update/README.md` 和 README 对应章节。
- 提交信息用 conventional 风格（feat/fix/build/deploy + 中文正文均可），工作分支 `dev`。
