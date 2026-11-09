<#
.SYNOPSIS
把 dsh-pet Hermes 插件装进 Hermes 的插件目录并启用：适配层 + 自带运行时。

.DESCRIPTION
复制到 <HERMES_HOME>\plugins\dsh-pet\ 的内容：
  - hermes/ 适配层（约 20 KB）
  - dsh-pet/ 的可分发部分（lib、runtime/electron-helper、素材、package.json，约 75 MB）
装完的插件是自包含的：运行时就在插件目录里，不再指向仓库路径；卸载时整目录一起带走。
只复制运行时要用的素材，跳过 assets/preview（那 67 MB 是 DSH 市场的预览图，插件用不到）。

.PARAMETER HermesHome
覆盖 HERMES_HOME（默认读环境变量，再退回 %LOCALAPPDATA%\hermes）。

.PARAMETER Force
已存在同名插件时先删掉再装（默认直接报错，避免手滑覆盖）。

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\hermes-plugin-install.ps1
#>
param(
    [string]$HermesHome = "",
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$pluginId = 'dsh-pet'

$repoRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $repoRoot 'hermes'
if (-not (Test-Path (Join-Path $source 'plugin.yaml'))) {
    throw "找不到 $source\plugin.yaml，请在仓库里运行这个脚本。"
}
$runtimeSource = Join-Path $repoRoot 'dsh-pet'
if (-not (Test-Path (Join-Path $runtimeSource 'lib\standalone.js'))) {
    throw "找不到 $runtimeSource\lib\standalone.js；先在 dsh-pet 里跑一次 npm run build。"
}

$home_ = Join-Path $env:LOCALAPPDATA 'hermes'
if ($HermesHome) { $home_ = $HermesHome }
elseif ($env:HERMES_HOME) { $home_ = $env:HERMES_HOME }
$destination = Join-Path $home_ "plugins\$pluginId"

$hermes = Get-Command hermes.exe -ErrorAction SilentlyContinue
if (-not $hermes) {
    $hermes = Get-Command hermes -ErrorAction SilentlyContinue
}

if (Test-Path $destination) {
    if (-not $Force) {
        throw "$destination 已存在。要覆盖请加 -Force。"
    }
    # 桌宠进程会锁住插件目录里的文件（尤其自带运行时的 lib 和 node_modules），
    # 不先让它退出，Remove-Item 会失败。
    if ($hermes) {
        & $hermes.Source $pluginId stop 2>$null | Out-Null
        Start-Sleep -Seconds 2
    }
    Remove-Item -LiteralPath $destination -Recurse -Force
}

New-Item -ItemType Directory -Path $destination -Force | Out-Null
Copy-Item -Path (Join-Path $source '*') -Destination $destination -Recurse -Force

# 运行时复制进插件自己的目录：装完的插件不再依赖仓库还在不在原路径。
$vendored = Join-Path $destination 'dsh-pet'
New-Item -ItemType Directory -Path $vendored -Force | Out-Null
foreach ($item in @('lib', 'runtime', 'package.json')) {
    Copy-Item -LiteralPath (Join-Path $runtimeSource $item) -Destination $vendored -Recurse -Force
}
$assetsOut = Join-Path $vendored 'assets'
New-Item -ItemType Directory -Path $assetsOut -Force | Out-Null
foreach ($item in @('webm', 'fonts', 'memes', 'pic', 'sound', 'config.jsonc', 'logo.png', 'main-character.png')) {
    $from = Join-Path $runtimeSource "assets\$item"
    if (Test-Path $from) {
        Copy-Item -LiteralPath $from -Destination $assetsOut -Recurse -Force
    }
}

$version = (Get-Content -LiteralPath (Join-Path $runtimeSource 'package.json') -Raw -Encoding UTF8 | ConvertFrom-Json).version

# lib/ 里还有几个没打进 bundle 的依赖（zod、openai、@electron/get 等），按 package.json 装生产依赖。
# 装完的 node_modules 就在插件目录里，运行时不依赖仓库、也不依赖用户全局环境。
$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npm) {
    throw "找不到 npm；装运行时依赖要用它。先把 Node.js 的 bin 放进 PATH。"
}
Write-Host "正在安装运行时依赖（约 90 个包，几十秒）..."
Push-Location $vendored
try {
    & $npm.Source install --omit=dev --ignore-scripts --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) {
        throw "npm install 失败（退出码 $LASTEXITCODE），插件目录里的依赖没装全。"
    }
} finally {
    Pop-Location
}
@{
    packageRoot   = $vendored
    sourceRepo    = $runtimeSource
    dshPetVersion = $version
    writtenAt     = (Get-Date).ToUniversalTime().ToString('o')
} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $destination 'package-path.json') -Encoding UTF8

$totalBytes = (Get-ChildItem -LiteralPath $destination -Recurse -File | Measure-Object -Property Length -Sum).Sum
$sizeMb = [math]::Round(($totalBytes / 1MB), 1)
Write-Host "已安装到 $destination（$sizeMb MB，dsh-pet $version 自带运行时）"

if ($hermes) {
    & $hermes.Source plugins enable --no-allow-tool-override $pluginId
    if ($LASTEXITCODE -ne 0) {
        throw "hermes plugins enable 失败（退出码 $LASTEXITCODE），插件已复制但没启用。"
    }
    Write-Host ""
    Write-Host "已启用。查状态：hermes $pluginId status"
    Write-Host "重启网关后自动拉起桌宠；不想让它自动出现就跑 hermes $pluginId stop。"
} else {
    Write-Host ""
    Write-Host "没找到 hermes 命令，请手动执行：hermes plugins enable $pluginId"
}