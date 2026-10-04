<p align="center">
  <img src="assets/brand/fqgate-logo.png" alt="FQGate" width="152">
</p>

<h1 align="center">FQGate</h1>

<p align="center">
  连接同花顺行情，在 AI 对话中查询和分析证券数据<br>
  免费使用 · 支持 Windows 和 macOS
</p>

<p align="center">
  <a href="https://github.com/fqgate/FQGate-releases/releases/latest"><img src="https://img.shields.io/github/v/release/fqgate/FQGate-releases?display_name=tag&sort=semver" alt="最新版本"></a>
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS-2563EB" alt="支持 Windows 和 macOS">
  <a href="https://gitee.com/qicuo/fqgate-releases"><img src="https://img.shields.io/badge/Gitee-国内下载-C71D23" alt="Gitee 国内下载"></a>
</p>

FQGate 是运行在你电脑上的行情工具。连接同花顺行情后，可以在 AI 对话中查询行情、查看 K 线、阅读证券资讯，或按条件筛选股票。FQGate 负责提供数据，具体分析由你使用的 AI 工具完成。

[下载最新版](https://github.com/fqgate/FQGate-releases/releases/latest) · [Gitee 国内下载](https://gitee.com/qicuo/fqgate-releases/releases) · [使用说明](./使用说明.md) · [问题反馈](https://github.com/fqgate/FQGate-releases/issues)

## 主要功能

| 功能       | 可以做什么                                                            |
| ---------- | --------------------------------------------------------------------- |
| 行情查询   | 查询证券价格、涨跌幅、分时、历史 K 线和板块行情                       |
| 深度行情   | 查询十档盘口、逐笔成交、逐笔委托等 Level-2 数据，需要账号具备相应权限 |
| 资讯与选股 | 查询证券资讯，使用同花顺问财按条件筛选股票                            |
| 自选股     | 同花顺直连：查看自选股，添加或移除证券，创建或删除自定义列表          |
| AI 接入    | 按页面提示连接常用 AI 工具，在对话中使用行情数据                      |
| 行情图表   | 在“应用中心”打开行情图表，可刷新数据、切换数据连接和置顶窗口          |

普通行情可使用游客身份；登录同花顺账号后，可使用该账号已有的数据权限。游客数据可能延迟或受限，问财和 Level-2 等功能还受账号权限及同花顺服务规则影响。

自选股管理只改变关注列表，不会买卖证券。FQGate 当前不提供券商账户登录、持仓查询、下单、撤单或资金划转。

## 下载与开始使用

请从[最新版本页面](https://github.com/fqgate/FQGate-releases/releases/latest)或 [Gitee 下载页面](https://gitee.com/qicuo/fqgate-releases/releases)选择适合电脑的文件。下表中的名称是文件名后半部分，前面还会包含版本号。

| 电脑类型                     | 选择的文件         | 打开方式                                     |
| ---------------------------- | ------------------ | -------------------------------------------- |
| Windows（Intel / AMD 64 位） | `windows-x64.zip`  | 完整解压到固定文件夹，再打开 `FQGate.exe`    |
| Apple 芯片 Mac               | `macos-arm64.zip`  | 解压后将 `FQGate.app` 移到“应用程序”，再打开 |
| Intel 芯片 Mac               | `macos-x86_64.zip` | 解压后将 `FQGate.app` 移到“应用程序”，再打开 |

Windows 建议下载 ZIP 压缩包，并保留解压后的全部文件，其中 `fqgate-updater.exe` 用于安装更新。Mac 可在苹果菜单的“关于本机”中查看芯片类型。

1. 打开 FQGate，阅读首次使用说明。
2. 进入“行情数据”，确认数据连接可用；需要账号权限时登录同花顺。
3. 进入“AI 接入”，选择你使用的 AI 工具并按提示连接。
4. 重新打开 AI 对话，尝试提问：“查询某只股票的最新行情，并注明数据时间。”

使用 AI 查询行情时，请保持 FQGate 运行。详细操作及常见问题见[使用说明](./使用说明.md)。

当前安装包可能触发 Windows 或 macOS 的安全提示。请确认文件来自本页提供的下载入口；下载页中的同名 `.sha256` 文件用于核对下载文件是否完整。

## 支持的 AI 工具

目前提供 ChatGPT（Codex 接入）、Claude Code、豆包、千问、DeepSeek Harness、WorkBuddy、ZCode 和 OpenClaw 的接入说明与配置功能。不同工具的安装条件和操作方式有所不同，请以“AI 接入”页面提示为准。

ChatGPT 的接入使用本机 Codex 功能，并非在 ChatGPT 网页或手机应用中直接添加 FQGate。豆包和千问目前提供 Windows 安装方式，需要先在相应工具中进入一次工作任务。

也可以在 [FQGate Agent](https://github.com/fqgate/FQGate-agent) 查看各 AI 工具的连接说明；国内访问可使用 [Gitee](https://gitee.com/qicuo/tonghuasun-agent)。

## 版本更新

在“更新”中查看版本说明和可用更新。新版本下载完成后，按提示确认安装。

正式版本自安装包生成之日起有效 30 天，下载或首次打开的日期不影响期限。到期后会停止行情服务并提示升级，请保持联网并及时更新。若启动后退出，请检查网络并获取最新版本；仍无法使用时，请[反馈问题](https://github.com/fqgate/FQGate-releases/issues)。

## 数据与隐私

FQGate 在你的电脑上运行，行情连接仅供本机使用。获取行情和检查更新需要联网；使用 AI 查询时，相应的数据会交给你选择的 AI 服务处理，请留意该服务的隐私设置。

反馈问题时，请勿公开密码、验证码、登录二维码或其他登录信息。“运行日志”中的文件保存功能默认关闭，需要时可自行选择保存位置并开启。

## 交流与支持

- QQ 群：[免费 AI 量化数据](https://qm.qq.com/q/ZQSuiYQZ4Q)，群号：`14546787`
- 安装、启动和更新问题：[FQGate 问题反馈](https://github.com/fqgate/FQGate-releases/issues)
- AI 连接和插件问题：[FQGate Agent 问题反馈](https://github.com/fqgate/FQGate-agent/issues)
- 项目主页：[FQGate](https://github.com/fqgate)

<p align="center">
  <a href="./assets/support.png">
    <img src="./assets/support.png" alt="支持 FQGate 与 FQGate Agent" width="100%">
  </a>
</p>

如果 FQGate 对你有帮助，欢迎自愿赞赏。赞赏不会解锁额外功能或数据权限，也不附带服务承诺。

## 使用须知

FQGate 免费使用，配套 AI 插件开放源代码，主程序不在插件的开源许可范围内。详见 [FQGate Agent 许可说明](https://github.com/fqgate/FQGate-agent/blob/main/docs/legal/README.md)。

FQGate 用于查询和展示数据，不提供投资建议或收益承诺。行情可能存在延迟或缺失，AI 分析也可能有误，请结合数据时间及原始来源判断。

FQGate 由独立开发者维护，与同花顺及其关联公司无授权、合作或背书关系。实际可用的数据以所连接账号的权限和服务范围为准。
