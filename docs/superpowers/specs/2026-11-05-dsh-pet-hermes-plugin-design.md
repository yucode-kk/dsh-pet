# dsh-pet Hermes 插件接入设计

> 状态：待用户审阅；本文件只定义接入范围，不代表已修改业务代码。

## 1. 目标

让当前 `dsh-pet` 项目既能作为 DSH 插件使用，也能作为 Hermes Agent 原生插件安装和启用。两种宿主使用同一份桌宠实现与资源，但分别运行独立实例，用户配置、AI 配置、对话记忆、待办、日志、运行状态、Electron 用户数据和缓存均不共享。

用户明确要求：只增加 Hermes 注册/适配能力；尽量只增不删；不改变现有 DSH 插件的安装、运行和数据行为；桌宠在 Hermes 中的实际功能和交互尽量与当前项目一致。

## 2. 现状与依据

### 2.1 项目已有可复用运行时

当前项目已经有独立运行入口，不需要重新开发一套 Node.js 桌宠核心：

- `dsh-pet/package.json` 已声明 `dsh-pet-standalone` 命令，入口为 `lib/standalone.js`。
- `dsh-pet/src/standalone/cli.ts` 已把伪宿主 context、本机 HTTP 服务、现有 host 插件逻辑和 Electron Helper 串成独立启动流程。
- 独立运行时通过现有 `apply(context.ctx)` 注册当前项目逻辑，不要求启动 DSH。
- 现有 HTTP API 已覆盖状态、工作状态、余额、碎碎念、广播、动画、睡眠/唤醒、聊天、配置等能力；具体接入应以仓库的 `openapi.yaml` 为准，不重新实现业务逻辑。
- `dsh-pet/src/standalone/request-security.ts` 已实现回环地址校验、随机能力令牌和请求授权；Hermes 适配层必须沿用该安全边界。

### 2.2 现有数据目录支持隔离

- `dsh-pet/src/host/helper/electron.ts` 中的 `dshHomeDir()` 优先读取进程环境变量 `DSH_HOME`，未设置时才回落到用户目录下的 `.dsh`。
- 独立运行时的配置路径、AI 存储、日志、运行状态文件等均以该 home 目录为根。
- `dsh-pet/src/standalone/runtime-state.ts` 会在 home 下写入 `dsh-pet/standalone-runtime.json`，保存当前进程、端口和本机访问令牌等运行信息。

因此 Hermes 适配器可以只给其启动的 Node 子进程设置独立 `DSH_HOME`，不需要改变 DSH 进程的环境变量或默认目录。实现时仍须逐项验证 Electron `userData`、下载目录和缓存是否都落在 Hermes 专属目录中。

## 3. Hermes 官方插件规范

接入目标是 Hermes 的 **Native General Plugin**，不是 Memory Provider、Context Engine、Platform Adapter，也不是 Hermes Desktop UI Plugin。

官方规范：

- 插件目录包含 `plugin.yaml` 与 Python `__init__.py`。
- `__init__.py` 提供 `register(ctx)`，通过 `ctx.register_tool()` 注册工具，通过 `ctx.register_hook()` 注册生命周期钩子。
- 用户插件通过 `hermes plugins install` 安装，再通过 `hermes plugins enable` 启用。
- 使用 `hermes plugins doctor <path> --ci` 验证清单解析、注册过程和工具/钩子声明。
- 使用 `plugin_data_dir("<plugin-id>")` 获取当前 Hermes Profile 专属、可跨插件更新保留的数据目录；插件运行状态不得写入插件安装目录。
- Hermes 支持插件管理的外部 sidecar 运行时；Node 依赖应留在插件自己的运行时目录，不能污染 Hermes 的 Python 依赖环境。

官方资料：

- https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins
- https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/
- https://hermes-agent.nousresearch.com/docs/reference/cli-commands

## 4. 选定方案：薄 Python 适配层 + 复用现有 Node 独立运行时

### 4.1 结构

在现有仓库中新增 Hermes 原生插件入口和少量 Python 适配代码。Hermes 负责发现、启用和宿主生命周期；Python 适配层负责启动/停止/检查现有 Node 独立运行时，并在需要时把 Hermes 的生命周期事件映射到当前项目已有的工作状态 API。桌宠现有设置页、菜单和交互仍是主要使用界面。

Node.js 侧继续使用当前 `dsh-pet` 包中的独立 CLI、host 逻辑、HTTP API、Electron Helper、设置页和素材。不得复制或重写桌宠核心。

### 4.2 启动与生命周期

- Hermes 插件启用后，按当前 DSH 插件的使用预期启动桌宠；启动动作必须非阻塞，不能卡住 Hermes 插件注册。
- Python 适配层启动现有独立 CLI 子进程，不在 Hermes 主进程内直接运行桌宠主循环。
- 子进程使用 Hermes 当前 Profile 的专属数据目录作为 `DSH_HOME`；只修改传给子进程的环境副本，不修改 Hermes 进程的全局环境。
- 通过现有运行状态文件和健康检查确认启动结果；不得硬编码端口，必须使用运行时实际分配的端口。
- Hermes 关闭或插件宿主退出时，适配层应尽力正常关闭自己启动的子进程；不得终止 DSH 实例或其他用户启动的桌宠进程。
- 重复注册、重复启动、启动失败、异常退出和清理必须幂等；不得误杀其他实例。

### 4.3 Hermes 注册内容

- 使用官方 `plugin.yaml` 声明 Native General Plugin 和实际注册的钩子；如果没有确实对应当前功能的模型可调用操作，就不声明 `provides_tools`，不为了“看起来像插件”而额外造工具。
- 使用 `register(ctx)` 注册适配层。
- 插件启用时启动现有桌宠运行时；现有设置页、菜单、聊天和配置方式保持不变。
- 现有工作状态功能如需接入 Hermes，应使用官方公开生命周期钩子（例如 `pre_llm_call`、`pre_tool_call`、`post_tool_call`、`post_llm_call`、`on_session_end`），将 Hermes 的任务/工具活动映射到当前项目已有的工作状态接口；不得通过 monkey-patch、改写 Hermes 内部模块或拦截并转发所有对话文本来实现。
- 不因接入 Hermes 而新增自动同步聊天记录、共享人设/记忆、额外通知、自动转发助手回复或新的模型可调用控制工具。
- 所有 hook 回调接受 `**kwargs`，遵守 Hermes 官方回调签名和返回约定。

## 5. 数据与进程隔离

### 5.1 数据目录

Hermes 插件使用：

```text
<HERMES_PROFILE_PLUGIN_DATA>/dsh-pet/
  runtime-home/
    dsh-pet/
      config.json
      logs.txt
      standalone-runtime.json
      ...其他现有运行数据...
  electron/
  cache/
```

实际目录布局以现有路径工具为准；以上仅表达隔离边界，不要求照搬目录名。

约束：

- Hermes 的 `DSH_HOME` 指向 Hermes 插件专属数据根。
- DSH 安装继续使用 DSH 自己的 Profile / `DSH_HOME`。
- 不从 DSH 目录复制配置、AI 密钥、聊天记录、记忆或待办到 Hermes。
- 不建立跨目录软链接，不复用同一个 Electron `userData` Profile。
- Hermes Profile 之间也必须隔离；使用 Hermes 官方 `plugin_data_dir()`，不自行拼接全局固定路径。
- 插件更新不得覆盖或清空用户运行数据；插件卸载的数据保留行为遵循 Hermes 官方插件存储规则。

### 5.2 同时运行

必须支持 DSH 桌宠与 Hermes 桌宠同时运行：

- 两个独立 Node 进程。
- 两套独立数据根和 Electron Profile。
- HTTP 服务各自监听回环地址，并使用各自随机令牌与实际端口。
- 两边停止、重启、更新或异常退出均不得影响另一边。
- 只允许共享只读程序代码和静态素材；不共享运行时状态、用户配置和数据库。

## 6. 不做的事情

- 不重写 TypeScript/Node 桌宠核心。
- 不重构或删除现有 DSH 插件入口、Cordis 注册、DSH 依赖和原有配置路径。
- 不修改 Hermes 核心源码。
- 不把项目改造成 Hermes Desktop 内嵌面板；现有 Electron 桌宠窗口保持原样。
- 不创建第二套动画、聊天、语音、配置或设置页面。
- 不增加用户未要求的自动消息转发、对话同步或数据迁移。
- 不让 Hermes 和 DSH 共用配置、记忆、密钥、待办或 Electron Profile。

## 7. 验收标准

1. `hermes plugins doctor <plugin-path> --ci` 通过；Hermes 能识别并注册声明的生命周期钩子（不额外创建模型工具）。
2. 能通过 Hermes 官方插件安装/启用流程安装；启用后可启动现有桌宠。
3. Hermes 桌宠继续使用当前项目已有设置页、动画、对话、语音和其他已支持功能，不出现第二套实现。
4. Hermes 的数据全部位于其当前 Profile 的插件数据根；修改 Hermes 配置不改变 DSH 配置，反之亦然。
5. DSH 与 Hermes 两个实例可同时运行；各自重启/退出不影响另一方。
6. Hermes 进程退出时，不遗留由该插件启动的 Node/Electron 进程；也不误停其他实例。
7. 现有 DSH 构建、测试和运行行为保持不变；新增改动只涉及 Hermes 适配与必要的、向后兼容的隔离参数。
8. 不发生用户数据复制、共享或迁移。

## 8. 方案比较

| 方案 | 处理方式 | 结论 |
| --- | --- | --- |
| Hermes Native Plugin 薄适配层 | Python `register(ctx)` 启动现有 Node standalone，并用公开 hooks 适配现有宿主生命周期 | **选定**。符合用户要求，复用当前实现，新增面最小 |
| 仅用 MCP Server | 通过 MCP 配置连接外部服务 | 不选。用户明确要求 Hermes 插件注册；已有官方 Python 插件 API 更直接 |
| 用 Python 重写桌宠 | 重写动画、HTTP 服务、Electron 和业务逻辑 | 不选。重复实现、维护双份逻辑，且容易影响 DSH |

## 9. 实施时待核实的工程细节

以下属于实现前检查，不改变本设计的目标：

- 确认当前 Git 仓库根与 Hermes 插件根的对应关系，确保官方 Git 安装能在插件根发现 `plugin.yaml` 和 `__init__.py`。
- 确认 Node sidecar 的官方安装/依赖声明方式，并复用现有 `dsh-pet` 构建产物；不得让 Hermes 的 Python 环境承担 Node 依赖。
- 逐项核验 Electron `userData`、Electron 下载缓存、AI 配置、日志、聊天记忆和待办的实际路径。
- 以现有 DSH 宿主生命周期接入和 `openapi.yaml` 为准，确定 Hermes hook 到现有工作状态 API 的最小映射；不把所有 HTTP API 包装成新工具。
- 核对 Hermes 当前公开 hook 名称和参数签名，所有回调兼容 `**kwargs`；不依赖私有模块路径。
