# FQGate：同花顺 MCP 服务与本地量化交易网关（QQ群：14546787）

FQGate（Fast Quant Gateway）是一款免费的 Windows 本地量化交易网关，也是一套面向 AI Agent 的同花顺 MCP 服务与交易 API。它可以把 A 股行情和券商交易能力连接给 Codex、Claude Code、WorkBuddy、豆包、千问等 AI 工具，让 AI 助手查询行情与账户数据；在你主动开启相应权限后，也可以使用委托下单、撤单和资金划转等功能。

> Tonghuashun (THS) market data MCP server and local quantitative trading API gateway for AI agents.

FQGate 不需要 AI 模拟鼠标点击交易窗口，也不依赖截图或文字识别读取结果。程序只在本机提供服务，并复用已经建立的登录和连接，适合个人量化研究、AI 股票工具、A 股数据查询和交易接口接入。

## FQGate 能做什么

| 功能 | 你可以使用的能力 |
| --- | --- |
| 行情数据 | 证券搜索、实时行情、历史行情、分时、K 线、盘口、板块、资讯、选股、期权和 Level-2 数据 |
| 账户查询 | 查询交易账户、资产、持仓、委托、成交、资金流水和交割单 |
| 委托下单 | 下单、撤单和新股申购；必须由你在主界面主动开启 |
| 资金划转 | 银证转账和担保品划转；必须由你在主界面主动开启 |
| 桌面通知 | 委托被券商受理或出现新增成交时发送通知 |
| AI 接入 | 提供标准 MCP、豆包兼容方式和 HTTP API，可由多个本机 AI 工具共用 |

普通行情在没有登录同花顺账号时可以自动使用游客行情。问财基础查询需要登录同花顺账号；Level-2 行情还要求账号已经开通相应权限。证券交易使用独立的券商账户，不与同花顺行情账号混用。

## 支持哪些 AI 工具

配套的开源 Agent 插件目前面向 Codex、Claude Code、WorkBuddy、豆包、千问、OpenClaw、ZCode 和 DeepSeek Harness。插件负责安装引导、AI 使用说明和对话中的交互界面，FQGate 负责你电脑上的行情、账户与交易服务。

- 国内访问：[Gitee - tonghuasun-agent](https://gitee.com/qicuo/tonghuasun-agent)
- GitHub：[zhuyifang/tonghuasun-agent](https://github.com/zhuyifang/tonghuasun-agent)

FQGate 主程序免费使用；配套 Agent 插件公开源代码。FQGate 主程序源码不在本仓库公开，本仓库只提供官方下载、版本信息和使用说明。

## 下载 FQGate

当前正式版是 [FQGate v0.1.1](https://github.com/zhuyifang/fqgate-releases/releases/tag/fqgate-v0.1.1)。

- Windows 电脑下载 `FQGate-0.1.1-windows-x64-UNSIGNED.zip`，解压后运行 `FQGate.exe`；也可以直接下载单文件 EXE。
- Apple 芯片 Mac 下载 `FQGate-0.1.1-macos-arm64-ADHOC.zip`。
- Intel 芯片 Mac 下载 `FQGate-0.1.1-macos-x86_64-ADHOC.zip`。

文件名、大小和 SHA-256 可以在 [稳定版清单](./releases/stable.json)中查看。安装完成后，为 FQGate 创建一个桌面快捷方式，方便以后启动。

> 当前 Windows 程序还没有商业代码签名，macOS 程序也没有经过 Apple 公证，系统可能显示安全提示。请只从本仓库的发行页面下载，并核对页面公布的文件校验值。

## 使用方法

1. 下载并解压 Windows 安装包，运行 `FQGate.exe`。
2. 首次启动时阅读并确认风险声明。
3. 根据需要开启账户查询、委托下单、资金划转或桌面通知。
4. 打开上方的 Agent 插件项目，按照对应 AI 工具的说明完成连接。

FQGate 默认只监听本机地址，不会把服务直接开放到公网。程序重新启动后会优先恢复之前成功使用的同花顺账号或游客行情身份，凭证失效时才会重新登录或自动轮换游客账号。

## 交流群

- QQ 群：[免费 AI 量化数据](https://qm.qq.com/q/ZQSuiYQZ4Q)，群号：`14546787`
- 微信群：使用微信扫描下方二维码，加入“同花顺 AI Agent 插件交流”群。

<p align="center">
  <img src="./assets/wechat-group.jpg" alt="同花顺 MCP、FQGate 和 AI 量化交易插件微信群二维码" width="260">
</p>

当前微信群二维码有效期至 2026 年 9 月 17 日。如果二维码已经失效，可以先加入 QQ 群，或到 Agent 插件项目提交 Issue 提醒更新。

## 支持项目

<p align="center">
  <a href="./assets/support.png">
    <img src="./assets/support.png" alt="支持免费的 FQGate 同花顺 MCP 与开源 AI 量化插件" width="100%">
  </a>
</p>

如果 FQGate 和开源 Agent 插件对你有帮助，欢迎自愿赞赏支持。赞赏不会解锁任何功能、数据权限、投资建议、问题处理优先级或后续服务承诺。

## 平台与更新

当前提供 Windows x64、Apple 芯片 Mac 和 Intel 芯片 Mac 三种下载包。Windows 包未签名，macOS 包采用 ad-hoc 签名，首次运行时请按系统提示确认。

使用或更新 FQGate 时遇到问题，可以在本仓库提交 Issue；安装或使用 AI 插件时遇到问题，可以到 [tonghuasun-agent](https://github.com/zhuyifang/tonghuasun-agent/issues) 项目反馈。

## 可复现的行情性能测试

仓库提供了[行情性能测试脚本](./docs/行情性能对比.md)，可以在同一台电脑和同一网络环境下，记录 FQGate、AKShare、easyquotation、mootdx 与 Tushare 执行常见 A 股行情快照查询时的成功状态、返回行数和耗时。

脚本不会上传测试结果。JSON 测试记录保留脱敏后的原始异常，供复查测试过程；自动生成的 Markdown 报告只列出人需要判断的状态、完整性、耗时，以及错误类型和发生次数，不展示具体响应。结果不计算项目排名；不支持或需要额外权限的接口也会记录。

## 安全与责任说明

FQGate 不提供投资建议，也不会替你判断交易是否合适。行情、网络、券商客户端和第三方服务可能延迟或异常；涉及真实账户的操作，请自行核对账户、证券代码、价格、数量和资金参数。
