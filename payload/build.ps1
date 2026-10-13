#requires -Version 7
<#
.SYNOPSIS
    Builds the RemoteEye payload (payload/RemoteEye/out/RemoteEye.elf) with SharpProspero.
.DESCRIPTION
    Needs a SharpProspero checkout (https://github.com/SvenGDK/SharpProspero) and its build prerequisites
    (.NET 10 SDK and clang in WSL; see SharpProspero's README). Point SHARPPROSPERO_ROOT at it, or pass -SdkRoot,
    or keep it next to this repository as ../SharpProspero.
.PARAMETER Send
    Send the built payload to the console's ELF loader (port 9021) afterwards.
.EXAMPLE
    ./payload/build.ps1 -Send -Ip 192.168.1.50
#>
param(
    [string]$Configuration = "Release",
    [string]$SdkRoot = "",
    [string]$Ip = "",
    [switch]$Send
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $SdkRoot) { $SdkRoot = $env:SHARPPROSPERO_ROOT }
if (-not $SdkRoot) { $SdkRoot = Join-Path (Split-Path -Parent (Split-Path -Parent $here)) "SharpProspero" }
if (-not (Test-Path (Join-Path $SdkRoot "build/build-app.ps1"))) {
    throw "Set SHARPPROSPERO_ROOT, or pass -SdkRoot. Looked in '$SdkRoot'."
}
$env:SHARPPROSPERO_ROOT = (Resolve-Path $SdkRoot).Path

$project = Join-Path $here "RemoteEye"
& (Join-Path $SdkRoot "build/build-app.ps1") `
    -ProjectPath (Join-Path $project "RemoteEye.csproj") `
    -Output Folder `
    -OutputFolder (Join-Path $project "out") `
    -Configuration $Configuration `
    -SdkRoot $SdkRoot `
    -Payload

if (-not $Send) { return }
if (-not $Ip) { throw "-Send needs -Ip <console address>." }

# elfldr takes the raw ELF on 9021 and does not answer; RemoteEye's own port (3320 + build tag) is the
# confirmation that it runs.
$elf = Join-Path $project "out/RemoteEye.elf"
$client = [System.Net.Sockets.TcpClient]::new($Ip, 9021)
try {
    $bytes = [IO.File]::ReadAllBytes($elf)
    $client.GetStream().Write($bytes, 0, $bytes.Length)
} finally { $client.Close() }
Write-Host "Sent $elf to ${Ip}:9021"
