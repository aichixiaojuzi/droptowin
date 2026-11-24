# DropToWin / 随手传 - 安装脚本
# 用法: install.bat                 正常安装（自动跟随系统语言）
#       install.bat -DryRun         只检查环境和路径，不改任何设置
#       install.bat -Lang en        强制英文界面（zh 为中文）
param(
    [switch]$DryRun,
    [string]$Lang = ''
)

$ErrorActionPreference = 'Stop'
$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$parent = Split-Path -Parent $here

# ---------------------------------------------------------------- 语言
if (-not $Lang) {
    try { $ui = [System.Globalization.CultureInfo]::InstalledUICulture.Name }
    catch { $ui = 'en' }
    if ($ui -like 'zh*') { $Lang = 'zh' } else { $Lang = 'en' }
}
$Lang = $Lang.ToLower()
if ($Lang -like 'zh*') { $Lang = 'zh' }
elseif ($Lang -like 'en*') { $Lang = 'en' }
else { $Lang = 'zh' }

# 双语文案：T "中文" "English"
function T($zh, $en) { if ($Lang -eq 'en') { return $en } return $zh }

function Say($msg, $color = 'Gray') { Write-Host $msg -ForegroundColor $color }
function Head($msg) { Write-Host ""; Say $msg 'Cyan' }

Write-Host ""
Say "==============================================" 'Cyan'
Say "   随手传  DropToWin" 'Cyan'
Say (T "   安装程序" "   Setup") 'Cyan'
Say "==============================================" 'Cyan'
if ($DryRun) { Say (T "   [试运行模式] 不会修改任何设置" "   [Dry-run] Nothing will be changed") 'Yellow' }

# ---------------------------------------------------------------- 1. Python
Head (T "[1/5] 查找 Python ..." "[1/5] Looking for Python ...")

$py = $null
$searchDirs = @(
    (Join-Path $env:LOCALAPPDATA 'Programs\Python'),
    'C:\Python*',
    'C:\Program Files\Python*',
    'C:\Program Files (x86)\Python*'
)

$found = @()
foreach ($d in $searchDirs) {
    if (Test-Path $d) {
        $found += Get-ChildItem -Path $d -Filter 'pythonw.exe' -Recurse -ErrorAction SilentlyContinue |
                  Where-Object { $_.FullName -notlike '*\WindowsApps\*' }
    }
}

if ($found.Count -gt 0) {
    # 取版本号最高的那个
    $py = ($found | Sort-Object FullName -Descending | Select-Object -First 1).FullName
}

if (-not $py) {
    Say (T "  没有找到 Python。" "  Python was not found.") 'Red'
    Say ""
    Say (T "  请先安装 Python 3.10 或更高版本：" "  Please install Python 3.10 or newer first:") 'Yellow'
    Say "  https://www.python.org/downloads/" 'Yellow'
    Say ""
    Say (T "  安装时务必勾选  Add Python to PATH" "  Make sure to tick 'Add Python to PATH' during setup") 'Yellow'
    if (-not $DryRun) { Start-Process 'https://www.python.org/downloads/' }
    exit 1
}
Say (T "  找到: $py" "  Found: $py") 'Green'

# ---------------------------------------------------------------- 2. 数据目录
Head (T "[2/5] 选择接收文件的文件夹 ..." "[2/5] Choose the folder for received files ...")

if ($DryRun) {
    $base = $parent
    Say (T "   试运行：使用当前目录 $base" "   Dry-run: using the current folder $base") 'Yellow'
}
else {
    Add-Type -AssemblyName System.Windows.Forms
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    $dlg.Description = (T "选择用来存放接收文件的文件夹（推荐非系统盘）" "Choose where received files go (a non-system drive is recommended)")
    $dlg.ShowNewFolderButton = $true
    if (Test-Path 'D:\') { $dlg.SelectedPath = 'D:\iPhone' } else { $dlg.SelectedPath = "$parent" }

    if ($dlg.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        Say (T "  已取消安装。" "  Installation cancelled.") 'Yellow'
        exit 0
    }
    $base = $dlg.SelectedPath
}
Say (T "  数据目录: $base" "  Data folder: $base") 'Green'

if (-not $DryRun) {
    foreach ($sub in @('收件箱', '文本', '发给我')) {
        New-Item -ItemType Directory -Force -Path (Join-Path $base $sub) | Out-Null
    }
    Say (T "  已创建 收件箱 / 文本 / 发给我" "  Created the inbox / text / outbox folders") 'Green'
}

# ---------------------------------------------------------------- 3. 写配置
Head (T "[3/5] 写入配置 ..." "[3/5] Writing config ...")

$cfgPath = Join-Path $here 'config.json'
$cfgObj  = [ordered]@{ base_dir = $base; port = 8899 }
$cfgJson = $cfgObj | ConvertTo-Json

if ($DryRun) {
    Say (T "   将写入 $cfgPath :" "   Will write $cfgPath :") 'Yellow'
    Say "   $cfgJson" 'DarkGray'
}
else {
    # 必须写不带 BOM 的 UTF-8，否则 Python 读 JSON 会报错
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($cfgPath, $cfgJson, $utf8NoBom)
    Say "   $cfgPath" 'Green'
}

# ---------------------------------------------------------------- 4. 防火墙
Head (T "[4/5] 配置防火墙 ..." "[4/5] Configuring the firewall ...")

$ruleName = 'DropToWin-8899'

# 清理旧版留下的规则，避免两条并存
$oldRule = Get-NetFirewallRule -DisplayName 'iPhoneBridge-8899' -ErrorAction SilentlyContinue
if ($oldRule -and -not $DryRun) {
    Remove-NetFirewallRule -DisplayName 'iPhoneBridge-8899' -ErrorAction SilentlyContinue
    Say (T "   已清理旧版防火墙规则 iPhoneBridge-8899" "   Removed the old firewall rule iPhoneBridge-8899") 'Yellow'
}

$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue

if ($existing) {
    Say (T "  规则已存在，跳过。" "  Rule already exists, skipping.") 'Green'
}
elseif ($DryRun) {
    Say (T "   将新增入站规则：TCP 8899，专用网络放行" "   Will add an inbound rule: TCP 8899, private networks") 'Yellow'
}
else {
    try {
        New-NetFirewallRule -DisplayName $ruleName `
            -Direction Inbound -Protocol TCP -LocalPort 8899 `
            -Action Allow -Profile Private, Domain -ErrorAction Stop | Out-Null
        Say (T "   已放行 TCP 8899（专用网络）" "   Allowed TCP 8899 (private networks)") 'Green'
    }
    catch {
        Say (T "   防火墙规则添加失败: $($_.Exception.Message)" "   Failed to add the firewall rule: $($_.Exception.Message)") 'Red'
        Say (T "   手机可能连不上。可稍后手动运行 allow-firewall.bat" "   The phone may not connect. You can run allow-firewall.bat later.") 'Yellow'
    }
}

# ---------------------------------------------------------------- 5. 自启 + 启动
Head (T "[5/5] 配置开机自启并启动服务 ..." "[5/5] Setting up auto-start and starting the service ...")

$startup = [Environment]::GetFolderPath('Startup')
$vbsPath = Join-Path $startup 'DropToWin.vbs'

# 清理旧版自启脚本
$oldVbs = Join-Path $startup 'iPhoneBridge.vbs'
if ((Test-Path $oldVbs) -and -not $DryRun) {
    Remove-Item $oldVbs -Force -ErrorAction SilentlyContinue
    Say (T "   已清理旧版自启脚本 iPhoneBridge.vbs" "   Removed the old startup script iPhoneBridge.vbs") 'Yellow'
}

$tpl = @'
' Auto-generated by install.ps1 - delete this file to disable auto-start.
Dim sh
Set sh = CreateObject("WScript.Shell")
py  = "__PY__"
scr = "__SCR__"
sh.Run """" & py & """ """ & scr & """", 0, False
'@
$vbsText = $tpl.Replace('__PY__', $py).Replace('__SCR__', (Join-Path $here 'watchdog.py'))

if ($DryRun) {
    Say (T "   将写入 $vbsPath" "   Will write $vbsPath") 'Yellow'
}
else {
    # 转成 CRLF + ANSI，VBScript 只认这种
    $vbsText = $vbsText -replace "`r?`n", "`r`n"
    [System.IO.File]::WriteAllText($vbsPath, $vbsText, [System.Text.Encoding]::Default)
    Say "   $vbsPath" 'Green'
}

if (-not $DryRun) {
    $alreadyUp = $false
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.Connect('127.0.0.1', 8899)
        $alreadyUp = $true
        $tcp.Close()
    }
    catch { }

    if ($alreadyUp) {
        Say (T "   服务已在运行，跳过启动" "   Service is already running, skipping") 'Green'
    }
    else {
        Start-Process -FilePath $py -ArgumentList "`"$(Join-Path $here 'watchdog.py')`""
        Start-Sleep -Seconds 4
        Say (T "   服务已启动" "   Service started") 'Green'
    }
}

# ---------------------------------------------------------------- 收尾
$ip = $null
try {
    # 优先取 192.168 网段，避免被代理软件的 10.x 虚拟网卡干扰
    $ip = (Get-NetIPAddress -AddressFamily IPv4 |
           Where-Object { $_.IPAddress -like '192.168.*' } |
           Select-Object -First 1).IPAddress
    if (-not $ip) {
        $ip = (Get-NetIPAddress -AddressFamily IPv4 |
               Where-Object { $_.IPAddress -like '10.*' } |
               Select-Object -First 1).IPAddress
    }
} catch { }
if (-not $ip) { $ip = '127.0.0.1' }

Write-Host ""
Say "==============================================" 'Green'
Say (T "   安装完成" "   Installation complete") 'Green'
Say "==============================================" 'Green'
Write-Host ""
Say (T "   手机访问地址：" "   Open this on your phone:")
Say "   http://${ip}:8899" 'Yellow'
Write-Host ""
if ($Lang -eq 'en') {
    Say "   Two more things on the phone:"
    Say "     1. Open the address in Safari, then Add to Home Screen"
    Say "     2. Put the same IP into both shortcuts"
}
else {
    Say "   手机上还要做两件事："
    Say "     1. Safari 打开上面的地址，添加到主屏幕"
    Say "     2. 快捷指令里的地址也要换成这个"
}
Write-Host ""

if (-not $DryRun) {
    Say (T "   正在打开配置页面..." "   Opening the setup page ...") 'DarkGray'
    $langQuery = ''
    if ($Lang -eq 'en') { $langQuery = '?lang=en' }
    Start-Process "http://${ip}:8899/setup${langQuery}"
}
