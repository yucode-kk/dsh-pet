# 在 Hermes Agent 里用 dsh-pet

> 返回 [README](../README.md)。DSH 那边的安装方式不受影响，见 [INSTALL](../INSTALL.md)。

`hermes/` 是同一套桌宠的 **Hermes 原生插件**入口：Hermes 负责发现、启用和生命周期。装的时候会把
`dsh-pet/` 的可分发部分（`lib/`、`runtime/electron-helper/`、素材）一起复制进插件目录，所以**装完的插件
是自包含的**——运行时就在 `<HERMES_HOME>\plugins\dsh-pet\dsh-pet\` 里，不再指向仓库路径，
卸载时整个目录一起走。动画、对话、余额、设置页、素材一份都不重写。
两边各跑自己的进程，数据根、配置、对话记忆、待办与日志互不可见；唯一共用的是 Electron 的 userData
目录（`%APPDATA%\dsh-pet-electron-helper`，dsh-pet 自己在 `runtime/electron-helper/main.js` 里硬编码的，
适配层没有改动它）。

## 它是怎么接上的

```
Hermes 网关 ──加载──> hermes/plugin.yaml + __init__.py
                        │
                        ├─ on_session_start / post_tool_call / on_session_end
                        │     → POST /dsh-pet-7340/work-status（external 槽，档位：thinking/working/…）
                        │
                        └─ 非阻塞拉起 ──> node <插件目录>/dsh-pet/lib/standalone.js
                                            DSH_HOME = <HERMES_HOME>/plugin-data/dsh-pet/runtime-home
```

- 不新增模型可调用工具：桌宠的交互仍然是它自己的窗口、菜单和设置页。
- 钩子只做“最小映射”：会话开始 → `thinking`，工具结束 → `working`（气泡写工具名），回合结束 → 清回空闲。
  清空挂在 `on_session_end` 上，不用 `post_llm_call`——后者每次模型调用都会触发，带工具调用的回合会闪。
- 另外有个看门狗：进行中的档位 120 秒没新事件就自动清回空闲（`DSH_PET_IDLE_TIMEOUT` 可调）。后台技能复核、
  临时子会话里的工具调用不一定走完整回合收尾，光靠钩子会让“工具 skill_manage”这种状态和动作一直循环挂在那儿。
  写状态走后台守护线程，Hermes 的回合不会被本机 HTTP 阻塞。
- 自动拉起只发生在 `hermes gateway run` 这个常驻进程里；`hermes plugins list` 这类一次性命令不会弹宠物。

## 桌面窗口里的吃白饭

上游的「吃白饭」原本只挂在 DSH 网页端的 `shell.overlay` 插槽上，靠宿主注册菜单、`/anim` 与 `/position`
遥控、占用锁排他——独立模式/插件模式的窗口是 Helper 自己画的精灵窗，不加载 DSH 的客户端 bundle，
所以那个玩法天生进不来。这里补了一份桌面版（`src/client/baifan/desktop.ts` →
`runtime/electron-helper/baifan.js`，全局 `window.PetBaifan`）：

- 右键菜单在「睡觉」下面多一项 `吃白饭`（配置里没写 `animations.baifan` 就不出现）；再点一次是 `吃白饭收场`。
- 演出在原窗口里跑：下落 → 期待 → 掉饭 → 侧身追 → 米饭飞向嘴边 → 空碗落地 → 头顶分数，吃满 6 份自动收场回随机链。
- 复用网页端的 `scene.ts`（米饭/分数/空碗）、`geometry.ts`（脚底、嘴、地面线）、`constants.ts`（节奏与台词）
  和 `assets.ts`（5 段动画槽），只把传输层换成直接点播自己的视频，不需要接口、锁与位置下发。
- 素材：`assets/webm` 里的 5 段动画 + `assets/pic/baifan/{bowl-full,bowl-empty,rice-ball}.png`，安装脚本都会复制。

排查这类纯前端玩法时给一个调试口：给 Helper 进程设 `DSH_PET_HELPER_DEBUG_PORT=9333`（例如
`DSH_PET_HELPER_DEBUG_PORT=9333 hermes dsh-pet start`），就能用 CDP 连
`http://127.0.0.1:9333/json/list` 读 DOM、点菜单、抓窗口截图；不设变量时不开端口。

## 装

```powershell
# 在仓库根执行（复制适配层 + dsh-pet 运行时并装生产依赖，合计约 160 MB，然后启用）
powershell -ExecutionPolicy Bypass -File scripts\hermes-plugin-install.ps1

# 已经装过、想更新：加 -Force
powershell -ExecutionPolicy Bypass -File scripts\hermes-plugin-install.ps1 -Force
```

复制进插件目录的是：`hermes/` 适配层 + `dsh-pet/{lib,runtime,package.json}` + `dsh-pet/assets/{webm,fonts,
memes,pic,sound,config.jsonc,logo.png,main-character.png}`（跳掉 `assets/preview`，那 67 MB 是 DSH 市场的
预览图，插件用不到）+ 在插件目录里跑 `npm install --omit=dev` 装出来的 `node_modules`（`lib/` 里 zod、openai、
`@electron/get` 这些没打进 bundle）。脚本会写一个 `package-path.json` 到插件目录，里面指向插件**自己**那份
`dsh-pet/`。装完重启网关或桌面端生效（网关没有热加载）。

> 为什么插件根是 `hermes/` 而不是仓库根：`hermes plugins doctor` 会把插件目录整棵复制到临时目录跑注册，
> 仓库根带着 node_modules 和素材有 2 GB，复制就会卡死；而且 `hermes plugins install <owner/repo>` 要求
> `plugin.yaml` 在克隆根，本仓库结构不满足。所以适配层单独成目录、用复制安装，安装时把运行时搬进插件目录。

前置：Node.js ≥ 22.12（`node -v` 看一 眼，安装脚本要用 `npm` 装运行时依赖，走当前 npm registry/mirror；
离线机器装不了），`dsh-pet/` 里跑过 `npm install && npm run build`（`lib/` 构建产物必须在，安装脚本会检查）。
Electron 不必预先下载：本机 `%USERPROFILE%\.dsh\electron` 有现成的话直接复用，没有才下。

### 从插件市场装（catalog）

市场条目装的是 `repo#hermes` 子目录：clone + checkout，只复制 `hermes/` 适配层，**不跑仓库根的 ps1**，
插件目录里没有自带运行时。这条路第一次启动会自己补：

```
<HERMES_HOME>/plugin-data/dsh-pet/runtime/node_modules/dsh-pet/   # npm ci（读插件自带的锁文件）到这里
```

- 装的是**锁文件**：`hermes/runtime-lock/package-lock.json`（lockfileVersion 3）把 `dsh-pet@0.4.7`
  连同全部 98 个间接依赖钉到精确版本 + `sha512` integrity，源固定为 `registry.npmjs.org`，
  用 `npm ci --omit=dev --ignore-scripts --no-audit --no-fund` 装。上游包自己的 `dependencies`
  是 `^` 范围，用 `npm install <包>` 每次首启都会重新解析依赖树，只有顶层那个包是钉住的。
- 固定版本 `dsh-pet@0.4.7`（`hermes_adapter/sidecar.py` 里的 `RUNTIME_VERSION`）＋锁文件，不是自更新：
  升级要提 PR 把版本、锁文件与条目 sha 一起 bump，reviewer 才看得见。
- 一次性下载约 80 MB，需要联网和 PATH 里的 `npm`；慢网络下可能几十秒到几分钟没反应，
  日志里会有一行「运行时缺失，按锁文件装 …」。
- 子进程只拿到**最小环境**（`PATH`/`PATHEXT`/`COMSPEC`/`SYSTEMROOT`/`WINDIR`/`SYSTEMDRIVE`/
  `TEMP`/`TMP`/`TMPDIR`/`USERPROFILE`/`HOMEDRIVE`/`HOMEPATH`/`HOME`/`APPDATA`/`LOCALAPPDATA`/
  `PROGRAMDATA`/`PROGRAMFILES`/`PROGRAMFILES(X86)`/`NUMBER_OF_PROCESSORS`/`PROCESSOR_ARCHITECTURE`/
  `LANG`/`LC_ALL`，加上 `DSH_HOME` 与显式 opt-in 的 `DSH_PET_ELECTRON_PATH`/`DSH_PET_ELECTRON_MIRROR`），
  不继承 Hermes 的环境——宿主里那些 provider key 不会流到安装时才取回来的代码里。
  `npm ci` 那份额外透传 `NPM_CONFIG_*`（registry/proxy/cache 是工具配置，不是凭据）。
- 找不到 `npm` 时不会静默失败，`start` 会报「没有找到 dsh-pet 运行时，也找不到 npm」；
  插件目录缺锁文件时报「缺运行时锁文件」，提示重装而不是退回 `npm install`。
  想把插件目录变成自包含的，就用仓库根那份安装脚本（上一条）。
- 市场装出来的运行时是上游 npm 包（`dsh-pet@0.4.7`），桌面版「吃白饭」等本仓库的新特性
  以那份包的版本为准；要跟着仓库走就用自包含安装。

## 日常操作

```powershell
hermes dsh-pet status      # 运行状态、pid、端口、设置页地址、数据目录
hermes dsh-pet start       # 手动拉起（CLI 退出不会把它带走）
hermes dsh-pet stop        # 只停进程，插件还装着
hermes dsh-pet uninstall -y [--purge]   # 停进程 + 禁用 + 移除插件
```

`status` / `start` 还会把端口段里**别的** dsh-pet 实例列出来（DSH 自己那只、或上次没退干净留下的孤儿）。
屏幕上多一只窗口、动画不跟着动，先看这里——十有八九是盯着另一只实例在看。

设置项（`plugins.entries.dsh-pet.settings`，桌面端「插件」页也能改）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `autostart` | `true` | 网关启动时自动拉起桌宠 |
| `node_path` | 空 | node 可执行文件路径，留空用 PATH |
| `port` | `3080` | 起始端口，被占用会自动顺延（实际端口看 `status`） |
| `dsh_pet_path` | 空 | 显式指定 dsh-pet 包路径；留空就用插件目录里自带的那份 `dsh-pet/` |

## 数据落在哪

```
<HERMES_HOME>/plugin-data/dsh-pet/
  runtime-home/
    dsh-pet/            # main-config.jsonc、pet/*.json、logs.txt、standalone-runtime.json
    electron/           # Electron 运行时（首次启动下载，约 350 MB）
  runtime/              # 仅市场安装路径会用到：npm ci（读 runtime-lock 锁文件）出来的运行时
```

和 DSH 的 `~/.dsh` 分开：配置、AI 密钥、对话记忆、待办、日志各走各的，也没有软链接互相指向。
两只能同时跑，各自重启/退出互不影响。

> 例外是 Electron 的 userData：`dsh-pet` 把窗口进程的名字写死成 `dsh-pet-electron-helper`，
> 所以两个实例共用 `%APPDATA%\dsh-pet-electron-helper` 这一个 Profile 目录（窗口状态、缓存之类）。
> 适配层没有改这一处，需要彻底隔离得改 `dsh-pet/runtime/electron-helper/main.js` 里的 `app.setName`。

## 卸载（三种入口，都只动 Hermes 这边）

```powershell
# ① 插件还加载得起来时（推荐）
hermes dsh-pet uninstall -y            # 数据保留；想连数据一起清加 --purge

# ② 插件坏了/加载不起来时的兜底
powershell -ExecutionPolicy Bypass -File scripts\hermes-plugin-uninstall.ps1 [-Yes] [-Purge]

# ③ 手动三步
hermes plugins disable dsh-pet
hermes plugins remove  dsh-pet        # 交互确认输入 y
# 可选：删掉 <HERMES_HOME>\plugin-data\dsh-pet
```

入口 ①② 会先按 `standalone-runtime.json` 里的 pid/令牌发退出请求，动手前先用 `/health` 核对 pid
（对不上就跳过，绝不按端口猜、也不会误停别人的进程）。入口 ③ 是纯手动路径，本身不停进程——
先 `hermes dsh-pet stop` 或直接关窗口。仓库源码（`hermes/`、`dsh-pet/`）任何入口都不会碰。

卸载跑完会复查一遍：进程退没退、端口放没放开。没退就按父子关系强制结束（Windows 用 `taskkill /T`），
省得留下「窗口还在、状态文件没了」的幽灵实例——那种窗口永远不会有动画。

## 测试

适配层有 pytest 用例，放在 `tests/hermes_plugin/`（**在插件目录之外**，安装脚本只复制 `hermes/`，
测试不会跟着装进 `HERMES_HOME`）。覆盖 `hermes/__init__.py` 与 `hermes_adapter/` 全部 6 个文件，
当前 134 个用例，行覆盖率与分支覆盖率都是 100%。

```powershell
python -m pip install pytest coverage
python -m coverage run --branch --source=hermes -m pytest tests/hermes_plugin
python -m coverage report -m
```

用例全部走替身：`Popen`、`/health`、`hermes plugins ...` 都被替换，跑测试不会真的拉起桌宠、不会改
`<HERMES_HOME>` 里的插件注册，也不会结束任何真实进程。少数用例会临时改写 `sys.platform` 覆盖
Windows / POSIX 两条分支（`monkeypatch` 自动还原）。

## 已知限制

- Electron 不必每次重下：本机 `%USERPROFILE%\.dsh\electron` 有现成的那份时直接复用（插件会把它的路径
  传给子进程，`status`/启动输出里能看到用的是哪一份）；只有本机确实没有时才下载约 350 MB 到插件数据
  目录，下完才出窗口——`status` 里能看到端口已经起来，说明服务先就绪、窗口后到。
- 插件是复制安装，自带的那份 `dsh-pet/` 是**快照**：改完 `dsh-pet/` 或 `hermes/` 的代码都要重跑一次安装
  脚本（`-Force`）才生效；`dsh-pet/` 尚未独立成 Git 仓库，所以暂时不能用 `hermes plugins update`。
- 卸载走 `hermes dsh-pet uninstall -y`：会先停进程，再让 `hermes plugins remove` 把插件目录
  （含自带运行时）整个删掉，`--purge` 连数据目录一起清。
- Hermes 侧只映射工作状态；DSH 的系统通知、趋势等依赖 DSH 宿主的能力在独立模式下不可用（桌宠自己会在
  设置页标注）。