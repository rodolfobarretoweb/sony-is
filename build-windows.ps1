$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$python = if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { (py -3.13 -c 'import sys; print(sys.executable)').Trim() }

function Find-Tool([string]$name) {
  if ($env:ChocolateyInstall) {
    $package = Join-Path $env:ChocolateyInstall 'lib\ffmpeg'
    if (Test-Path $package) {
      $binary = Get-ChildItem $package -Filter $name -Recurse -File | Select-Object -First 1
      if ($binary) { return $binary.FullName }
    }
  }
  $tool = (Get-Command $name -ErrorAction Stop).Source
  if ($tool -match '\\chocolatey\\bin\\') {
    throw "Found a Chocolatey shim for $name, but not the real executable."
  }
  return $tool
}

$ffmpeg = Find-Tool 'ffmpeg.exe'
$ffprobe = Find-Tool 'ffprobe.exe'
& $python -c 'import tkinter; import _tkinter'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.13 with Tk is required.' }
& $python -m venv .build-venv
if ($LASTEXITCODE -ne 0) { throw 'Could not create the build environment.' }
$buildPython = Join-Path $PSScriptRoot '.build-venv\Scripts\python.exe'
& $buildPython -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw 'Could not install build dependencies.' }
& $buildPython -m unittest -q test_stabilize test_app
if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }

& $buildPython -m PyInstaller --noconfirm --clean --onedir --windowed `
  --name 'Sony Video Stabilizer' `
  --add-binary "${ffmpeg}:." `
  --add-binary "${ffprobe}:." `
  app.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
$frozenExe = Join-Path $PSScriptRoot 'dist\Sony Video Stabilizer\Sony Video Stabilizer.exe'
$smoke = Start-Process -FilePath $frozenExe -ArgumentList '--worker --help' -PassThru -Wait
if ($smoke.ExitCode -ne 0) { throw 'The packaged worker failed to start.' }

$compiler = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $compiler) {
  $compiler = Get-ChildItem ${env:ProgramFiles(x86)} -Directory -Filter 'Inno Setup*' -ErrorAction SilentlyContinue |
    ForEach-Object { Join-Path $_.FullName 'ISCC.exe' } | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $compiler -or -not (Test-Path $compiler)) { throw 'Install Inno Setup to create the Windows installer.' }
& $compiler 'installer-windows.iss'
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed.' }
Write-Host 'Created dist\Sony-Video-Stabilizer-Setup.exe'
