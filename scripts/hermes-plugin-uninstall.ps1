<#
.SYNOPSIS
把 dsh-pet 的 Hermes 插件卸干净：先收尾桌宠进程，再让 Hermes 自己摘插件。

.DESCRIPTION
依次做四件事：
  1) 按插件的运行时状态文件给桌宠发退出请求（收不到就只杀那一个 pid）；
  2) hermes plugins disable dsh-pet；
  3) hermes plugins remove  dsh-pet；
  4) 数据目录默认保留，-Purge 才删。
仓库里的源码（hermes/ 与 dsh-pet/）不会被碰。

.PARAMETER Purge
连插件数据目录一起删（配置、日志、Electron 运行时都在里面）。

.PARAMETER Yes
不再二次确认。

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\hermes-plugin-uninstall.ps1
#>
param(
    [switch]$Purge,
    [switch]$Yes,
    [string]$HermesHome = ""
)

$ErrorActionPreference = 'Stop'
$pluginId = 'dsh-pet'

$home_ = Join-Path $env:LOCALAPPDATA 'hermes'
if ($HermesHome) { $home_ = $HermesHome }
elseif ($env:HERMES_HOME) { $home_ = $env:HERMES_HOME }
$pluginDir = Join-Path $home_ "plugins\$pluginId"
$dataRoot = Join-Path $home_ "plugin-data\$pluginId"
$stateFile = Join-Path $dataRoot "runtime-home\dsh-pet\standalone-runtime.json"

Write-Host "卸载 dsh-pet Hermes 插件："
Write-Host "  插件目录  $pluginDir"
Write-Host "  数据目录  $dataRoot（$(if ($Purge) { '会删除' } else { '保留' })）"
if (-not $Yes) {
    $answer = Read-Host '继续？[y/N]'
    if ($answer -notin @('y', 'Y', 'yes')) {
        Write-Host '已取消，什么都没动。'
        exit 1
    }
}

# ① 先收尾桌宠：只认插件自己数据目录里的那个 pid，不按端口猜。
if (Test-Path $stateFile) {
    try {
        $runtime = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json

        # 状态文件可能是上次崩溃留下的，pid 会被系统复用；先确认这只真是桌宠，再动手。
        $verified = $false
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$($runtime.port)/health" -TimeoutSec 2
            $verified = ($health.pid -eq $runtime.pid)
        } catch { }
        if (-not $verified) {
            $procInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$($runtime.pid)" -ErrorAction SilentlyContinue
            if ($procInfo -and $procInfo.CommandLine -like '*dsh-pet*standalone*') { $verified = $true }
        }

        if (-not $verified) {
            Write-Host "  · 桌宠：状态文件里的 pid $($runtime.pid) 对不上，跳过，没动任何进程"
        } else {
            $uri = "http://127.0.0.1:$($runtime.port)/dsh-pet-7340/quit"
            Invoke-RestMethod -Method Post -Uri $uri -Body '{}' -ContentType 'application/json' `
                -Headers @{ 'x-dsh-pet-token' = $runtime.token } -TimeoutSec 3 | Out-Null
            Write-Host "  · 桌宠：已发退出请求（pid $($runtime.pid)）"
            $deadline = (Get-Date).AddSeconds(15)
            while ((Get-Date) -lt $deadline -and (Get-Process -Id $runtime.pid -ErrorAction SilentlyContinue)) {
                Start-Sleep -Milliseconds 200
            }
            if (Get-Process -Id $runtime.pid -ErrorAction SilentlyContinue) {
                Stop-Process -Id $runtime.pid -Force
                Write-Host "  · 桌宠：优雅退出超时，已强制结束 pid $($runtime.pid)"
            }
        }
    } catch {
        Write-Host "  · 桌宠：收尾请求失败（$($_.Exception.Message)），继续卸载"
    }
} else {
    Write-Host '  · 桌宠：没有运行时状态文件，按没在跑处理'
}

# ②③ 走官方命令，不手改 config.yaml。
$hermes = Get-Command hermes.exe -ErrorAction SilentlyContinue
if (-not $hermes) { $hermes = Get-Command hermes -ErrorAction SilentlyContinue }
if ($hermes) {
    & $hermes.Source plugins disable $pluginId 2>$null | Out-Null
    'y' | & $hermes.Source plugins remove $pluginId 2>&1 | Out-Host
} else {
    Write-Host '  ! 没找到 hermes 命令，请手动执行：'
    Write-Host "    hermes plugins disable $pluginId"
    Write-Host "    hermes plugins remove  $pluginId"
}

# ③.5 复查：状态文件那只真的退了、端口真的放开了。
# 幽灵实例就是这样漏掉的——状态文件没了/进程还在，窗口继续在屏幕上装宠物。
if ($runtime) {
    if (Get-Process -Id $runtime.pid -ErrorAction SilentlyContinue) {
        Write-Host "  · 复查：进程 $($runtime.pid) 还在，按父子关系强制结束"
        & taskkill /PID $runtime.pid /T /F 2>$null | Out-Null
        Start-Sleep -Milliseconds 600
        if (Get-Process -Id $runtime.pid -ErrorAction SilentlyContinue) {
            Write-Host "  ! 进程 $($runtime.pid) 仍然活着，请手动结束"
        } else {
            Write-Host "  · 复查：进程已结束"
        }
    } else {
        Write-Host "  · 复查：进程已退出"
    }
    $listening = $false
    try {
        $listening = [bool](Get-NetTCPConnection -LocalPort $runtime.port -State Listen -ErrorAction SilentlyContinue)
    } catch { }
    if ($listening) { Write-Host "  · 复查：端口 $($runtime.port) 仍有监听（可能是别的实例）" }
    else { Write-Host "  · 复查：端口已放开" }
}

# ④ 数据目录
if ($Purge) {
    if (Test-Path $pluginDir) {
        Write-Host "  · 数据目录：插件目录还在（摘除没成功），数据先留着，别剪在跑的实例"
    } elseif (Test-Path $dataRoot) {
        Remove-Item -LiteralPath $dataRoot -Recurse -Force
        Write-Host "  · 数据目录：已删除 $dataRoot"
    }
} else {
    Write-Host "  · 数据目录：保留在 $dataRoot"
    Write-Host "    （重新装回来会接着用这份数据；想清干净加 -Purge）"
}

Write-Host '完成。仓库源码没动过；插件名会在桌面端/网关重启后从列表里彻底消失。'