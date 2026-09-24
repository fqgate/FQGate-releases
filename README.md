<p align="center">
  <img src="assets/brand/fqgate-logo.png" alt="FQGate 品牌标识" width="152">
</p>

<h1 align="center">FQGate Releases</h1>

<p align="center">
  FQGate 官方发行与下载仓库<br>
  为 Windows 与 macOS 提供可校验的正式安装包和稳定版元数据
</p>

<p align="center">
  <a href="https://github.com/fqgate/FQGate-releases/releases/latest"><img src="https://img.shields.io/github/v/release/fqgate/FQGate-releases?display_name=tag&sort=semver" alt="Latest Release"></a>
  <a href="https://github.com/fqgate/FQGate-releases/actions"><img src="https://img.shields.io/github/actions/workflow/status/fqgate/FQGate-releases/ci.yml?label=release%20checks" alt="Release Checks"></a>
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS-2563EB" alt="Windows and macOS">
  <a href="https://gitee.com/qicuo/fqgate-releases"><img src="https://img.shields.io/badge/Gitee-国内镜像-C71D23" alt="Gitee 国内镜像"></a>
</p>

> FQGate 已启用专属 GitHub 组织 [fqgate](https://github.com/fqgate)。后续开源协作、正式发行和品牌建设均以该组织为长期入口。

本仓库是 FQGate 的官方发行入口，负责保存正式安装包、稳定版清单、文件大小和 SHA-256 校验信息。这里不包含 FQGate 私有主源码，也不会要求你在配置文件中填写项目维护者的密钥。

FQGate（Fast Quant Gateway）是一款免费的 Windows、macOS 本地行情数据网关。它把同花顺行情连接给 Codex、Claude Code、WorkBuddy、豆包、千问等 AI 工具，让你在对话中查询 A 股实时行情、K 线、资讯、选股和 Level-2 数据。

> Tonghuashun (THS) market data MCP server and local market data API gateway for AI agents.

## 项目入口

| 项目 | 用途 | GitHub | 国内镜像 |
| --- | --- | --- | --- |
| FQGate Releases | 官方安装包、稳定版清单与校验信息 | [fqgate/FQGate-releases](https://github.com/fqgate/FQGate-releases) | [Gitee](https://gitee.com/qicuo/fqgate-releases) |
| FQGate Agent | 开源技能、宿主适配、安装器、MCP Apps 与 SDK | [fqgate/FQGate-agent](https://github.com/fqgate/FQGate-agent) | [Gitee](https://gitee.com/qicuo/tonghuasun-agent) |
| FQGate 组织 | 项目主页与后续开源项目 | [github.com/fqgate](https://github.com/fqgate) | — |

## FQGate 能做什么

| 能力 | 说明 |
| --- | --- |
| 行情数据 | 证券搜索、A 股实时行情、历史行情、分时、K 线、盘口、板块、资讯、选股、期权和 Level-2 数据 |
| 登录恢复 | 按需恢复已保存的同花顺账号或游客行情身份 |
| MCP Apps | 查看已安装的行情界面及版本，界面可以独立更新 |
| AI 接入 | 提供标准 MCP、豆包兼容方式和本机 HTTP API，可由多个 AI 工具共用 |

当前稳定版 FQGate 专注行情与资讯，不提供券商登录、交易账户查询、下单、撤单或资金划转 API。普通行情在没有登录同花顺账号时可以使用游客行情；问财基础查询需要登录同花顺账号；Level-2 数据还要求账号已经开通相应权限。游客行情的数据可能延迟或受限。

FQGate 不需要 AI 模拟鼠标点击，也不依赖截图或文字识别读取结果。程序默认只在本机提供服务，并复用已经建立的登录和数据连接，适合个人量化研究、AI 股票工具和 A 股数据查询。

## 下载 FQGate

当前正式版是 [FQGate v1.0.3](https://github.com/fqgate/FQGate-releases/releases/tag/fqgate-v1.0.3)。

- Windows 电脑下载 `FQGate-1.0.3-windows-x64-UNSIGNED.zip`，解压后运行 `FQGate.exe`；也可以直接下载单文件 EXE。
- Apple 芯片 Mac 下载 `FQGate-1.0.3-macos-arm64-ADHOC.zip`。
- Intel 芯片 Mac 下载 `FQGate-1.0.3-macos-x86_64-ADHOC.zip`。

文件名、大小和 SHA-256 可以在 [稳定版清单](./releases/stable.json)中查看。安装完成后，为 FQGate 创建一个桌面快捷方式，方便以后启动。

> 当前 Windows 程序还没有商业代码签名，macOS 程序也没有经过 Apple 公证，系统可能显示安全提示。请只从本仓库的发行页面下载，并核对页面公布的文件校验值。

## 使用方法

1. 在上方正式发行页选择与你的操作系统和处理器架构匹配的安装包。
2. 根据 [稳定版清单](./releases/stable.json)核对文件大小和 SHA-256，再解压并启动 FQGate。
3. 首次启动时阅读并确认风险声明；需要完整行情权限时登录同花顺账号。
4. 为 FQGate 创建桌面快捷方式，方便以后启动本机网关。
5. 打开 [FQGate Agent](https://github.com/fqgate/FQGate-agent)，按照对应 AI 工具的说明完成连接。
6. 确认 FQGate 已启动、AI 工具中的 `fqgate` 连接成功，并且能够读取工具列表或完成健康检查。

FQGate 默认只监听本机地址，不会把服务直接开放到公网。程序重新启动后会优先恢复之前成功使用的同花顺账号或游客行情身份，凭证失效时才会重新登录或自动轮换游客账号。

## 平台与版本校验

| 平台 | 架构 | 当前发行形式 |
| --- | --- | --- |
| Windows | x64 | ZIP 压缩包与单文件 EXE |
| macOS | Apple Silicon / arm64 | ZIP 压缩包，ad-hoc 签名 |
| macOS | Intel / x86_64 | ZIP 压缩包，ad-hoc 签名 |

机器可读的 1.x 当前稳定版信息保存在 [`releases/stable.json`](./releases/stable.json)，1.x 客户端以它核对 `version`、`fileName`、`size` 和 `sha256`。这个入口只属于 1.x，不会被 2.0 发布流程改写。FQGate 2.0 使用同一发行仓库下独立的 `releases/v2/freshness.json` 签名通道，两代产品的发布工作流、元数据合同和稳定入口彼此隔离。

正式安装包只会发布在标签为 `fqgate-v<版本>` 的 GitHub Release 中。已经公开的 Release 不会被后续构建覆盖；不同平台的同一版本会汇总到同一个 Release，便于统一核验。

## Agent 与开源范围

配套的 [FQGate Agent](https://github.com/fqgate/FQGate-agent) 面向 Codex、Claude Code、WorkBuddy、豆包、千问、OpenClaw、ZCode 和 DeepSeek Harness，负责安装引导、AI 工具适配、技能与交互界面。

FQGate Agent 的开源组件依据其仓库中的 AGPL-3.0-only 许可证发布。FQGate 主程序免费使用，但主源码不在本仓库公开；编译包适用随包许可。两个仓库的职责和许可边界相互独立。

## 安全与隐私

- 只从 [GitHub 官方发行页](https://github.com/fqgate/FQGate-releases/releases)或 [Gitee 国内镜像](https://gitee.com/qicuo/fqgate-releases/releases)下载安装包。
- 安装前核对稳定版清单中的 SHA-256；校验失败时不要运行文件。
- FQGate 默认监听本机地址，除非你清楚网络暴露带来的风险，否则不要自行转发到公网。
- 项目维护者不会通过本仓库收集你的登录凭证、行情查询结果或本机配置。

使用云端 AI 服务时，发送给该服务的对话和工具结果可能受其隐私政策与设置约束。请根据自己使用的 AI 工具判断可以提交的数据范围。

## 交流与反馈

- QQ 群：[免费 AI 量化数据](https://qm.qq.com/q/ZQSuiYQZ4Q)，群号：`14546787`
- FQGate 安装、启动或发行包问题：[本仓库 Issues](https://github.com/fqgate/FQGate-releases/issues)
- AI 工具安装与插件问题：[FQGate Agent Issues](https://github.com/fqgate/FQGate-agent/issues)

## 支持项目

<p align="center">
  <a href="./assets/support.png">
    <img src="./assets/support.png" alt="支持 FQGate 与开源 Agent 项目" width="100%">
  </a>
</p>

如果 FQGate 和开源 Agent 项目对你有帮助，欢迎自愿赞赏支持。赞赏不会解锁任何功能、数据权限、投资建议、问题处理优先级或后续服务承诺。

## 责任说明

FQGate 是数据连接与展示工具，不提供个股推荐、收益预测或投资建议。AI 生成的内容可能存在错误或延迟，行情及证券信息请以数据提供方、证券公司和交易所的正式记录为准。

这是一个由独立开发者维护的非官方项目，与同花顺及其关联公司不存在授权、合作或背书关系。FQGate 不会增加任何账号的数据权限，实际可用范围仍以相应账号及服务权限为准。
