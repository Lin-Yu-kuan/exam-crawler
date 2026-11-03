"""產生本機直連下載腳本（Windows .bat / PowerShell .ps1 / Shell .sh）。

供雲端版或本機版將選定的考科與路徑對照表匯出成輕量腳本，
由使用者本機直接透過系統內建 curl 直連考選部下載至 Downloads 目錄，
免除雲端二次下載與伺服器記憶體／頻寬負擔。
"""

from __future__ import annotations

import os
from collections import OrderedDict
from pathlib import Path


def generate_windows_batch(
    items: list[tuple[str, str]],
    default_dir: str = r"%USERPROFILE%\Downloads\國考題庫",
) -> str:
    """產生 Windows .bat 批次檔，調用系統內建 curl.exe 實現本機直存。

    :param items: 清單元素為 (url, relative_path)
    :param default_dir: 本機下載目標根目錄（預設為使用者的 Downloads\國考題庫）
    """
    lines: list[str] = [
        "@echo off",
        "rem ========================================================",
        "rem  國考刷題神器 - 本機一鍵直連下載腳本 (Windows Batch)",
        "rem  由考選部 (moex.gov.tw) 官方伺服器直接下載至本機",
        "rem ========================================================",
        "chcp 65001 > nul",
        "",
        f'set "TARGET_ROOT={default_dir}"',
        'echo 正在檢查並建立題庫目標資料夾: "%TARGET_ROOT%"',
        'if not exist "%TARGET_ROOT%" mkdir "%TARGET_ROOT%"',
        'cd /d "%TARGET_ROOT%"',
        "",
        "echo 正在檢查系統 curl 工具...",
        "where curl >nul 2>nul",
        "if %errorlevel% neq 0 (",
        '    echo [錯誤] 找不到系統 curl 指令。Windows 10/11 應內建此工具。',
        "    pause",
        "    exit /b 1",
        ")",
        "",
        "echo 開始批次下載試題與答案（已存在的檔案將自動略過）...",
        "echo.",
    ]

    # 記錄 URL 首次下載的目的地，若後續有相同 URL 則改用 copy 加速（共用去重）
    url_first_path: dict[str, str] = {}
    total = len(items)

    for index, (url, rel_path) in enumerate(items, start=1):
        # 轉為 Windows 反斜線
        win_rel = rel_path.replace("/", "\\")
        dir_name = os.path.dirname(win_rel)

        lines.append(f"rem [{index}/{total}] {win_rel}")
        if dir_name:
            lines.append(f'if not exist "{dir_name}" mkdir "{dir_name}"')

        lines.append(f'if exist "{win_rel}" (')
        lines.append(f'    echo [{index}/{total}] [已存在略過] {win_rel}')
        lines.append(") else (")

        if url in url_first_path:
            source_path = url_first_path[url]
            lines.append(f'    if exist "{source_path}" (')
            lines.append(f'        echo [{index}/{total}] [共用附件複製] {win_rel}')
            lines.append(f'        copy /y "{source_path}" "{win_rel}" >nul')
            lines.append("    ) else (")
            lines.append(f'        echo [{index}/{total}] [下載中] {win_rel}')
            lines.append(f'        curl -f -s -S -L -k --retry 3 "{url}" -o "{win_rel}"')
            lines.append("    )")
        else:
            url_first_path[url] = win_rel
            lines.append(f'    echo [{index}/{total}] [下載中] {win_rel}')
            lines.append(f'    curl -f -s -S -L -k --retry 3 "{url}" -o "{win_rel}"')

        lines.append(")")
        lines.append("")

    lines.extend(
        [
            "echo.",
            "echo ========================================================",
            "echo  下載與歸檔完成！共處理 %TARGET_ROOT%",
            "echo ========================================================",
            "pause",
            "",
        ]
    )

    return "\r\n".join(lines)


def generate_powershell_script(
    items: list[tuple[str, str]],
    default_dir: str = "$HOME/Downloads/國考題庫",
) -> str:
    """產生跨平台 PowerShell 腳本 (.ps1)。"""
    lines: list[str] = [
        "# ========================================================",
        "#  國考刷題神器 - 本機一鍵直連下載腳本 (PowerShell)",
        "# ========================================================",
        "$OutputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
        f'$TargetRoot = "{default_dir}"',
        'Write-Host "目標題庫目錄: $TargetRoot" -ForegroundColor Cyan',
        'if (!(Test-Path -Path $TargetRoot)) { New-Item -ItemType Directory -Path $TargetRoot -Force | Out-Null }',
        'Set-Location -Path $TargetRoot',
        "",
        "$Items = @(",
    ]

    for url, rel_path in items:
        # PowerShell 中以單引號跳脫
        esc_url = url.replace("'", "''")
        esc_path = rel_path.replace("'", "''")
        lines.append(f"    @{{ Url = '{esc_url}'; Path = '{esc_path}' }}")

    lines.extend(
        [
            ")",
            "",
            "$total = $Items.Count",
            "$index = 0",
            "$urlCache = @{}",
            "",
            "foreach ($item in $Items) {",
            "    $index++",
            "    $dest = [System.IO.Path]::Combine($TargetRoot, $item.Path)",
            "    $parent = [System.IO.Path]::GetDirectoryName($dest)",
            "    if (!(Test-Path -Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }",
            "",
            "    if (Test-Path -Path $dest) {",
            '        Write-Host "[$index/$total] [已存在略過] $($item.Path)" -ForegroundColor DarkGray',
            "        continue",
            "    }",
            "",
            "    if ($urlCache.ContainsKey($item.Url) -and (Test-Path -Path $urlCache[$item.Url])) {",
            '        Write-Host "[$index/$total] [共用附件複製] $($item.Path)" -ForegroundColor Yellow',
            "        Copy-Item -Path $urlCache[$item.Url] -Destination $dest -Force",
            "    } else {",
            '        Write-Host "[$index/$total] [下載中] $($item.Path)" -ForegroundColor Green',
            '        curl.exe -f -s -S -L -k --retry 3 $item.Url -o $dest',
            "        if ($LASTEXITCODE -eq 0) { $urlCache[$item.Url] = $dest }",
            "    }",
            "}",
            "",
            'Write-Host "下載與歸檔完成！" -ForegroundColor Cyan',
            'Read-Host "按 Enter 鍵結束..."',
            "",
        ]
    )

    return "\n".join(lines)

