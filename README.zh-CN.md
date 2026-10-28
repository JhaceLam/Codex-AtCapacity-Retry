# Codex-AtCapacity-Retry

[English](README.md)

Codex App 出现 **“Selected model is at capacity”** 最终失败时，立即重试。
**不增加等待，不设次数上限**，保持原模型、原会话，直到成功或你点停止。

这是独立于 App 安装包的社区工具，支持 Mac 本地会话和可选的 Linux SSH 后台，仅使用 Python 标准库。

## 安装

需要 Python **3.9+**。本地 App 接入使用 macOS，远端后台使用 Linux；不支持 Windows。
请先安装 Codex 并完成登录，本工具沿用已有 CLI 和认证。

```sh
git clone https://github.com/JhaceLam/Codex-AtCapacity-Retry.git
cd Codex-AtCapacity-Retry
python3 patch_manager.py apply
```

首次安装后，彻底退出并重新打开 App 一次，使新连接经过重试包装器。
后续维护不会重启 App、共享后台或正在执行的任务。

使用非默认安装路径时：

```sh
python3 patch_manager.py apply --app /path/to/Codex.app
python3 patch_manager.py apply --codex /path/to/original/codex
```

`--codex` 应指向原始、未包装的 CLI。默认会发现常见的 Codex.app / ChatGPT.app 布局；非标准 Linux 安装可能需要显式指定 CLI。

## 可选：SSH 后台

在 Mac 上指定你自己的 SSH 别名：

```sh
python3 patch_manager.py apply --host my-devbox --host my-other-server
```

主机列表只保存在本机配置中，不进入仓库；默认不连接任何远程主机。
SSH 应已支持免交互连接，远端需要 Python 3.9+ 和可用的 Codex CLI。
此命令也会安装 Mac 本地接入。

直接在 Linux 执行 `apply` 只安装该主机的后台包装器。
从 Mac 配置的远端会由 Mac 的 launchd 定期维护；独立 Linux 部署可将 `ensure` 接入自己的更新流程。

## 检查、应用和回滚

确保 `~/.local/bin` 在 shell 的 PATH 中：

```sh
codex-capacity-patch status --all
codex-capacity-patch apply --all
codex-capacity-patch rollback --all
```

`--all` 只包含你已配置的 SSH 别名，不加则只处理当前机器。
也可以用 `--host SSH_ALIAS` 指定远程目标。
重复应用不会改动已正确的文件；`ensure` 不会在回滚后偷偷重新启用补丁。
回滚后重开 App 即完全使用原入口，源码、备份和回滚记录会保留。

## 重试原理

```text
Codex App → 重试包装器 → 原 Codex 后台
                 │
          收到最终容量失败
                 └→ 立即向同一会话发送空的继续请求
```

- 识别最终 `turn/completed` 容量失败，再发起 `turn/start` 空输入继续请求。
- 不切模型、不复制原提示词，沿用会话已有模型、权限、目录和模式。
- 容量重试路径没有 sleep、计时器、退避或次数上限。
- 成功、其他错误或点击停止后结束；停止也处理“下一次重试刚启动”的竞态。
- 普通 CLI 命令与非容量错误不受影响。

工具处理的是后台已经报告的最终失败，不改变报告之前 Codex 内部的重试、网络耗时或服务端可用性。

## 升级后的维护

补丁放在 App 目录外，版本化保存在：

```text
~/.local/lib/codex-capacity-retry/releases/<version>/
```

`current` 指向启用版本。为兼容已有安装，内部路径和 `codex-capacity-patch` 命令保持稳定。

Mac 的 launchd 在登录时恢复 `CODEX_CLI_PATH`，监听 App 与入口变化，并每 60 秒检查本地和配置过的远端。
**60 秒是升级检查周期，容量错误的重试不等待这个周期。**

安装器覆盖远端入口时，会先保存升级后的真实 CLI，再恢复包装器。
回滚会保留 CLI 的升级结果。CLI 版本或文件身份变化时，会重新校验相关 app-server 协议 schema。

升级过程中已经绕过包装器的连接，需要在下次重连后接入恢复后的入口。
未知的未来协议或无法识别的自定义启动器可能需要新版补丁；检查失败会明确报告。

## 补丁包、测试与日志

[GitHub Releases](https://github.com/JhaceLam/Codex-AtCapacity-Retry/releases) 提供独立安装压缩包、统一 diff `.patch` 和 SHA256SUMS。
安装前会校验 manifest 中的版本与文件 SHA-256。

```sh
python3 -m unittest -v
python3 make_bundle.py
```

测试使用模拟后台，不向模型发请求，覆盖连续 10,000 次容量错误、两种协议、停止竞态、升级覆盖后的恢复及回滚等场景。

Mac 重试日志：`~/Library/Logs/codex-capacity-retry/events.jsonl`。
Linux 重试日志：`~/.codex/log/capacity-immediate-retry.jsonl`。
Mac 维护记录：`~/Library/Logs/codex-capacity-retry/patch-maintenance.jsonl`。
日志大小有界，只记录事件、ID、次数等信息，不记录提示词、输出或令牌。

## 许可证

[MIT](LICENSE)。
