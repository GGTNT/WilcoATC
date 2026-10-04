<#
.SYNOPSIS
    Build WilcoATC into a folder that runs on a machine with no Python.

.DESCRIPTION
    Produces dist\WilcoATC: the executable, a console executable beside it,
    and everything the radio needs to run except the voices and the recogniser
    weights, which the program downloads on first run in the languages the
    pilot chose. Copy that folder anywhere and it runs.

    Run this from a Windows machine. PyInstaller does not cross-compile: it
    packages the interpreter it is running under, so a Windows build has to be
    made on Windows.

    An unsigned build gets called a virus. Not by every machine and not every
    time, but often enough that it is the first thing people report. -Sign is
    the fix; see README, "Windows Defender says it is a virus", for what the
    other switches buy when there is no certificate to sign with.

.EXAMPLE
    .\build.ps1
    .\build.ps1 -Zip
    .\build.ps1 -Zip -Sign 9A1B2C3D4E5F60718293A4B5C6D7E8F901234567
    .\build.ps1 -Zip -Sign certs\wilcoatc.pfx -RebuildBootloader
#>

[CmdletBinding()]
param(
    # Also produce dist\WilcoATC-win64.zip, ready to attach to a release.
    [switch]$Zip,
    # Skip regenerating the icons (needs Pillow).
    [switch]$SkipIcons,
    # The interpreter to package. Must be 64-bit Windows Python.
    [string]$Python = ".venv\Scripts\python.exe",

    # --- the antivirus half, all optional ---------------------------------
    #
    # A code-signing certificate is the only complete answer to Defender
    # calling the build a trojan. Everything else on this list reduces the
    # odds; a signature from a certificate with reputation ends it.

    # A .pfx file, or the SHA-1 thumbprint of a certificate already in the
    # certificate store. Without it the build goes out unsigned, as before.
    [string]$Sign,
    # Only for a .pfx. Prompted for if the file needs one and this is absent.
    [string]$PfxPassword,
    # A signature with no countersignature expires with the certificate, and
    # every copy already downloaded stops verifying on that day.
    [string]$TimestampUrl = "http://timestamp.digicert.com",
    # Compile PyInstaller's bootloader here instead of using the one that
    # came in the wheel. The prebuilt bootloader is byte-identical across
    # every PyInstaller program in the world, including the malware, which is
    # what a signature match is looking at. Needs MSVC build tools.
    [switch]$RebuildBootloader
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Step($text) { Write-Host "`n=== $text" -ForegroundColor Cyan }

if (-not (Test-Path $Python)) {
    throw "No interpreter at $Python. Create one first:`n" +
          "  python -m venv .venv`n" +
          "  .venv\Scripts\python -m pip install -r requirements.txt"
}

Step "Build dependencies"
& $Python -m pip install --upgrade --quiet pip
& $Python -m pip install --quiet -r requirements.txt
& $Python -m pip install --quiet -r requirements-build.txt

if ($RebuildBootloader) {
    Step "Bootloader"
    # Every PyInstaller wheel carries the same prebuilt bootloader, so every
    # program frozen with it opens with the same few hundred kilobytes of
    # machine code -- the malware frozen with it included, which is how
    # scanners came to match on those bytes alone. Compiling it here gives a
    # bootloader that behaves identically and does not match.
    & $Python -m pip install --force-reinstall --no-binary pyinstaller pyinstaller
    if ($LASTEXITCODE -ne 0) {
        throw "the bootloader did not compile. It needs the MSVC build tools " +
              "(Visual Studio Build Tools, C++ workload). Build without " +
              "-RebuildBootloader to use the one from the wheel."
    }
}

Step "Icons"
if ($SkipIcons) {
    Write-Host "skipped"
} else {
    try { & $Python scripts\make_icons.py }
    catch { Write-Warning "make_icons.py failed, keeping the icons already in the tree" }
}

Step "Freezing"

# Build to one side and swap it in at the end, rather than deleting the last
# good folder and hoping. A half-written bundle is not an obviously broken
# thing: the exe is the first file PyInstaller writes and the DLLs come
# after, so what is left when a freeze dies partway is a program that starts
# and then says a DLL is missing -- which reads as "the build is fine, my
# machine is broken", and is the single most confusing way for this to fail.
$staging = "dist-staging"
Remove-Item -Recurse -Force build, $staging -ErrorAction SilentlyContinue

# PyInstaller writes its whole INFO log to stderr, and this script runs with
# ErrorActionPreference = Stop. PowerShell turns a native command's stderr
# into ErrorRecords whenever that stream is captured -- by a wrapper, by CI,
# by Tee-Object, by `*>&1` -- and Stop then aborts the build on the *first
# log line*, before a single file is written. That looked exactly like a
# successful build that produced a broken folder. The exit code is the only
# thing here that actually says whether it worked, so ask that instead.
$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & $Python -m PyInstaller --noconfirm --clean --distpath $staging wilcoatc.spec
} finally {
    $ErrorActionPreference = $previous
}
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$staged = Join-Path $staging "WilcoATC"

# What a complete bundle has. Checked by name rather than by counting files,
# because the thing being guarded against is precisely a folder that has
# most of them.
$required = @(
    "WilcoATC.exe",
    "WilcoATC-console.exe",
    "_internal\base_library.zip",
    "_internal\VCRUNTIME140.dll"
)
$missing = @($required | Where-Object { -not (Test-Path (Join-Path $staged $_)) })
# The interpreter's own DLL is named for the Python that was packaged, which
# is not necessarily the one running this script, so it is matched by shape.
$interpreter = @(Get-ChildItem (Join-Path $staged "_internal") -Filter "python3*.dll" `
    -ErrorAction SilentlyContinue)
if ($interpreter.Count -eq 0) { $missing += "_internal\python3XX.dll" }
if ($missing.Count -gt 0) {
    throw ("PyInstaller reported success but the bundle is incomplete -- " +
           "missing: " + ($missing -join ", ") + ". The last good dist\ has " +
           "been left alone; $staged holds the partial one.")
}

Remove-Item -Recurse -Force dist -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path "dist" | Out-Null
Move-Item $staged "dist\WilcoATC"
Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue

$out = "dist\WilcoATC"

Step "Navigation data"
# The database ships with the build but sits outside the bundle: setup rebuilds
# it and overrides.csv is edited by hand, and neither works on a file sealed
# inside a read-only executable.
$navSrc = "data\navdata"
$navDst = "$out\data\navdata"
if (Test-Path "$navSrc\nav.sqlite") {
    New-Item -ItemType Directory -Force -Path $navDst | Out-Null
    Copy-Item "$navSrc\*" $navDst -Recurse -Force
    Write-Host ("copied {0:N0} MB of navigation data" -f ((Get-ChildItem $navDst -Recurse |
        Measure-Object Length -Sum).Sum / 1MB))
} else {
    Write-Warning "no data\navdata\nav.sqlite in the tree -- the build will ask the pilot to run setup"
}

Step "First-run note"

# What the note says about the signature has to match what was actually done
# to the file, because a pilot reading "not signed" on a signed build learns
# to ignore the rest of the paragraph as well.
$signatureNote = if ($Sign) {
@"
Windows may still hold the download for a moment the first few times: a new
signing certificate has no reputation yet, and it builds one by being run.
More info -> Run anyway.
"@
} else {
@"
Windows will warn that the publisher is unknown, because this build is not
signed. More info -> Run anyway.
"@
}

@"
WilcoATC
========

Double-click WilcoATC.exe.

The first start downloads the controller voices and the speech recogniser --
a few hundred megabytes, once, in the languages you pick. Everything after
that is local: no account, no subscription, nothing sent anywhere.

WilcoATC-console.exe is the same program with a terminal attached, for:

    WilcoATC-console.exe setup      choose languages and download what they need
    WilcoATC-console.exe doctor     check audio, recogniser, voices, simulator
    WilcoATC-console.exe freq LFPG  every published frequency at a field

Your settings, voices and logs live in this folder, next to the program. Copy
the folder to another machine and everything travels with it. If you put the
program somewhere Windows will not let it write -- Program Files -- it uses
%LOCALAPPDATA%\WilcoATC instead.

$signatureNote

If Windows Defender quarantines WilcoATC.exe as a trojan, it is wrong, and
Microsoft would like to know: upload the file at
https://www.microsoft.com/wdsi/filesubmission and pick "Incorrectly detected
as malware". It is normally corrected within a day or two, for everyone.

To fly before then, open Windows Security -> Virus & threat protection ->
Protection history, find the entry, and choose Restore. Then add this folder
under Manage settings -> Exclusions. Do that only for a copy you downloaded
from the project's own releases page, and only after checking the file at
https://www.virustotal.com -- a handful of engines calling it a packer is
what a PyInstaller build looks like; thirty of them naming the same trojan is
not.

If you unpacked the zip with Windows' own extractor and the window will not
open, Windows has marked the files as coming from the internet. The program
removes the marks itself at startup; if it cannot, open PowerShell in this
folder and run:

    Get-ChildItem -Recurse | Unblock-File

Unpacking with 7-Zip avoids the whole thing.
"@ | Set-Content "$out\FIRST-RUN.txt" -Encoding UTF8

Copy-Item README.md "$out\README.md" -Force -ErrorAction SilentlyContinue
# The README is the overview; the full manual is what a pilot looks things up in.
Copy-Item docs\MANUAL.md "$out\MANUAL.md" -Force -ErrorAction SilentlyContinue

Step "Runtime configuration"

# Windows' zip extractor marks every file it unpacks as coming from the
# internet, and the .NET Framework refuses to load a managed assembly wearing
# that mark -- which is Python.Runtime.dll, which is the window. The program
# strips the marks at startup, but it cannot strip them from a folder it may
# not write to, so the runtime it hosts is told here to load them regardless.
# Both executables need one: the config the CLR reads is the one named after
# the process.
$appConfig = @"
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <runtime>
    <loadFromRemoteSources enabled="true" />
  </runtime>
</configuration>
"@
foreach ($exe in "WilcoATC.exe", "WilcoATC-console.exe") {
    [System.IO.File]::WriteAllText(
        (Join-Path (Resolve-Path $out) "$exe.config"),
        $appConfig,
        (New-Object System.Text.UTF8Encoding $false))
}

Step "Signing"

# An Authenticode signature is the only thing on this list that settles the
# question rather than improving the odds. Defender's machine-learning
# classifiers weigh publisher identity heavily, SmartScreen builds reputation
# against the signing certificate rather than against each new build's hash,
# and a signed release therefore stops being a stranger after the first few
# hundred downloads instead of starting again from zero every version.
if (-not $Sign) {
    Write-Warning ("not signed. Windows will call the publisher unknown, and " +
        "Defender may call the file a trojan -- an unsigned PyInstaller build " +
        "that hooks the keyboard for push-to-talk and opens the microphone is " +
        "the exact shape its classifiers are trained on. Pass -Sign with a " +
        "certificate. See README, 'Windows Defender says it is a virus'.")
} else {
    # signtool is not on PATH and there is one copy per installed SDK. The
    # newest is the one that knows the current digest and timestamp options.
    $signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin" `
            -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '\\x64\\' } |
        Sort-Object FullName | Select-Object -Last 1
    if (-not $signtool) {
        throw "-Sign was given but signtool.exe is not installed. It comes " +
              "with the Windows SDK (Windows Kits\10\bin\<version>\x64)."
    }

    # SHA-256 for the file digest and for the countersignature: SHA-1 is no
    # longer trusted, and /tr is the RFC 3161 timestamp rather than the
    # obsolete /t, which is what keeps the signature valid after the
    # certificate expires.
    $signArgs = @("sign", "/fd", "SHA256", "/td", "SHA256", "/tr", $TimestampUrl)
    if (Test-Path -LiteralPath $Sign -PathType Leaf) {
        $signArgs += @("/f", (Resolve-Path $Sign).Path)
        if ($PfxPassword) { $signArgs += @("/p", $PfxPassword) }
    } else {
        # Not a file, so it is the thumbprint of a certificate already in the
        # store. Windows shows thumbprints with spaces in them; strip anything
        # that is not a hex digit rather than making that the caller's problem.
        $signArgs += @("/sha1", ($Sign -replace '[^0-9A-Fa-f]', ''))
    }

    foreach ($exe in "WilcoATC.exe", "WilcoATC-console.exe") {
        $path = Join-Path (Resolve-Path $out) $exe
        & $signtool.FullName @signArgs $path
        if ($LASTEXITCODE -ne 0) { throw "signing $exe failed" }
        $sig = Get-AuthenticodeSignature $path
        Write-Host ("{0}: {1} -- {2}" -f $exe, $sig.Status,
            $sig.SignerCertificate.Subject) -ForegroundColor Green
    }
}

$size = (Get-ChildItem $out -Recurse | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("`n{0} is {1:N0} MB" -f $out, $size) -ForegroundColor Green

if ($Zip) {
    Step "Zipping"
    $zipPath = "dist\WilcoATC-win64.zip"
    Remove-Item $zipPath -ErrorAction SilentlyContinue
    Compress-Archive -Path $out -DestinationPath $zipPath -CompressionLevel Optimal
    Write-Host ("{0} is {1:N0} MB" -f $zipPath, ((Get-Item $zipPath).Length / 1MB)) -ForegroundColor Green
}

Write-Host "`nTest it before shipping it:" -ForegroundColor Yellow
Write-Host "  $out\WilcoATC-console.exe doctor"
Write-Host "  $out\WilcoATC.exe"
