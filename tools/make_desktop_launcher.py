"""生成桌面启动器 `启动口播编排台.bat`（GBK + CRLF）。

    python tools/make_desktop_launcher.py

为什么 `.bat` 不直接手写、而要过一个生成器：

  · **编码是硬要求**。本机 cmd 按 GBK 读 `.bat`。用普通编辑器保存成 UTF-8，
    中文（项目路径里有"AI学习"）会变成乱码 —— 表现是"找不到项目目录"，
    看着像文件被删了，其实是编码。本脚本产出的字节有保证。
  · **换行也是硬要求**。裸 LF 会让 cmd 的多行 `if (...)` 与 `goto` 失效。
  · 内容里要写 Windows 路径（`\\binaries`、`\\langgraph_app`），
    在源码里必须用 raw 字符串，否则 `\\b` 之类会被 Python 当成转义符悄悄吃掉。

所以：**改动一律改本文件、再重新运行它**；不要直接编辑桌面上那个 `.bat`。
写完会立刻回读校验（BOM / CRLF / 能否按 GBK 解码），不通过就以非零码退出。

注意：内容里只能用 **GBK 能编码的字符**。`⚠️`（U+26A0）、`⇒`（U+21D2）这类符号
编不进去，会在写盘**之前**直接报 `UnicodeEncodeError` —— 有意让它早失败，
而不是写出一半才发现桌面上的文件是坏的。
"""

from pathlib import Path

DESKTOP = Path.home() / "Desktop"
TARGET = DESKTOP / "启动口播编排台.bat"

# 内容里有 `\binaries`（`\b` 会被当成退格符）、`\langgraph_app` 这类路径片段，
# 所以**必须**是 raw 字符串，否则会被 Python 悄悄转义掉。
CONTENT = r'''@echo off
chcp 936 >nul
setlocal enableextensions
rem ============================================================
rem  口播剪辑编排台 一键启动器 —— 放桌面双击即可
rem  项目：AI学习\koubo-agent-dsh  （LangGraph 编排层 + Web 界面）
rem  服务：http://127.0.0.1:8760/
rem  本文件为 GBK 编码 + CRLF 换行
rem
rem  【注意】开头那句 chcp 936 是必需的，不是装饰：
rem     cmd 是边执行边按"当前控制台代码页"重新读这个文件的。
rem     若从 UTF-8 代码页的终端（新版 Terminal / 被改过 OutputEncoding 的会话）
rem     启动，GBK 的中文路径会被按 UTF-8 解成乱码，路径判定就会失败，报"找不到项目"。
rem     所以先把自己钉回 936，双击与终端两条路都成立。
rem     另外：chcp 必须放在任何中文之前，否则前面几行仍然读错。
rem     改编码会导致同样的后果；改换行（CRLF 换成 LF）会让多行 if 与 goto 失效。
rem ============================================================
title 口播剪辑编排台

set "PROJ=%~dp0AI学习\koubo-agent-dsh"
set "PY=C:\Users\lenovo\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "HOST=127.0.0.1"
set "PORT=8760"
set "URL=http://127.0.0.1:8760/"

echo.
echo   口播剪辑编排台
echo   ==========================================
echo.

if not exist "%PROJ%\langgraph_app\webapp\server.py" (
    echo   [错误] 找不到 %PROJ%\langgraph_app\webapp\server.py
    echo          项目目录可能被移动或改名了。
    pause
    exit /b 1
)
if not exist "%PY%" (
    echo   [错误] 找不到 Python：%PY%
    pause
    exit /b 1
)

echo   [1/3] 检查是否已在运行 ...
powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; try { $null = Invoke-WebRequest -Uri 'http://%HOST%:%PORT%/api/meta' -UseBasicParsing -TimeoutSec 3; exit 0 } catch { exit 1 }"
if not errorlevel 1 (
    echo         已在运行，直接打开页面。
    start "" "%URL%"
    echo.
    echo   这次没有启动新进程。要停服务，请关掉那个最小化的命令行窗口。
    echo.
    echo   本窗口 6 秒后自动关闭。
    ping -n 7 127.0.0.1 >nul 2>&1
    exit /b 0
)

echo   [2/3] 检查磁盘 ...
powershell -NoProfile -Command "$f=(Get-PSDrive C).Free/1GB; Write-Host ('         项目所在盘剩余 {0:N1} GB（一次完整跑约需 1 GB）' -f $f); if ($f -lt 1.5) { Write-Host '         [警告] 空间偏低，可能跑不完一次完整剪辑' }"

echo   [3/3] 启动服务（最小化窗口运行）...
cd /d "%PROJ%"
start "口播编排台" /min "%PY%" -m langgraph_app.webapp.server %HOST% %PORT%
powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; for($i=0;$i -lt 30;$i++){ try { $null = Invoke-WebRequest -Uri 'http://%HOST%:%PORT%/api/meta' -UseBasicParsing -TimeoutSec 2; Write-Host '         OK - 已就绪'; exit 0 } catch { Start-Sleep -Seconds 1 } }; Write-Host '         [警告] 30 秒内未就绪，请看那个最小化的窗口'"

start "" "%URL%"
echo.
echo   完成。编排台：%URL%
echo   演练模式不花钱；真实运行的付费动作会停下来等你拍板。
echo.
echo   注意：关掉那个最小化的命令行窗口 = 停掉服务。
echo.
echo   本窗口 6 秒后自动关闭。
ping -n 7 127.0.0.1 >nul 2>&1
exit /b 0
'''


def main() -> int:
    if not DESKTOP.is_dir():
        print(f"桌面目录不存在：{DESKTOP}")
        return 1
    text = CONTENT.replace("\r\n", "\n").replace("\n", "\r\n")
    data = text.encode("gbk")           # ⚠️ GBK，且**不加 BOM**
    TARGET.write_bytes(data)

    # 立刻回读校验：编码、换行、能否按 GBK 解回来
    back = TARGET.read_bytes()
    ok_gbk = True
    try:
        back.decode("gbk")
    except UnicodeDecodeError:
        ok_gbk = False
    print(f"已写出：{TARGET}")
    print(f"  字节数      : {len(back)}")
    print(f"  BOM         : {back[:3] == b'\xef\xbb\xbf'}")
    print(f"  CRLF 行数   : {back.count(b'\r\n')}")
    print(f"  裸 LF 行数  : {back.count(b'\n') - back.count(b'\r\n')}（必须为 0）")
    print(f"  可 GBK 解码 : {ok_gbk}")
    return 0 if (ok_gbk and back.count(b"\n") == back.count(b"\r\n")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
