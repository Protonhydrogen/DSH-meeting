<#
.SYNOPSIS
    用 Windows SAPI 把一段 JSON 脚本合成为若干 WAV 文件，并输出时长清单。

.DESCRIPTION
    学术会议自测需要"已知文本 + 已知时间轴"的多说话人音频。本脚本只负责
    合成单个语句；时间轴拼接由 Python 侧完成（精确可控）。

    输入 JSON：{ "items": [ { "text": "...", "voice": "Microsoft Huihui Desktop",
                             "rate": 0, "out": "D:\\...\\utt_000.wav" } ] }
    输出 JSON：{ "ok": true, "items": [ { "out": ..., "ms": 1234, "voice": ... } ],
                "errors": [ ... ] }

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\make_test_audio.ps1 `
        -SpecPath spec.json -ManifestPath manifest.json
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SpecPath,
    [Parameter(Mandatory = $true)][string]$ManifestPath
)

$ErrorActionPreference = 'Stop'

function ConvertTo-Hashtable($obj) {
    if ($null -eq $obj) { return $null }
    if ($obj -is [System.Collections.IEnumerable] -and $obj -isnot [string]) {
        return @($obj | ForEach-Object { ConvertTo-Hashtable $_ })
    }
    if ($obj -is [psobject] -and $obj.PSObject.Properties.Count -gt 0 -and $obj -isnot [string]) {
        $h = @{}
        foreach ($p in $obj.PSObject.Properties) { $h[$p.Name] = ConvertTo-Hashtable $p.Value }
        return $h
    }
    return $obj
}

try {
    Add-Type -AssemblyName System.Speech
} catch {
    @{ ok = $false; errors = @("Add-Type System.Speech failed: $($_.Exception.Message)") } |
        ConvertTo-Json -Depth 6 | Set-Content -Path $ManifestPath -Encoding UTF8
    exit 2
}

$spec = Get-Content -Path $SpecPath -Raw -Encoding UTF8 | ConvertFrom-Json
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$results = @()
$errors = @()

foreach ($item in $spec.items) {
    try {
        if ($item.voice) { $synth.SelectVoice([string]$item.voice) }
        $rate = 0
        if ($null -ne $item.rate) { $rate = [int]$item.rate }
        $synth.Rate = [Math]::Max(-10, [Math]::Min(10, $rate))
        $out = [string]$item.out
        $dir = Split-Path -Parent $out
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        if (Test-Path $out) { Remove-Item $out -Force }
        $synth.SetOutputToWaveFile($out)
        $synth.Speak([string]$item.text)
        $synth.SetOutputToNull()
        $len = (Get-Item $out).Length
        $ms = 0
        if ($len -gt 44) {
            # 16-bit mono 22.05 kHz PCM WAV：字节数换算时长
            $ms = [int](($len - 44) / 2 / 22050 * 1000)
        }
        $results += @{ out = $out; ms = $ms; bytes = $len; voice = [string]$item.voice }
    } catch {
        $errors += "item failed: $($_.Exception.Message)"
    }
}
$synth.Dispose()

@{ ok = ($errors.Count -eq 0); items = $results; errors = $errors } |
    ConvertTo-Json -Depth 6 | Set-Content -Path $ManifestPath -Encoding UTF8
exit 0
