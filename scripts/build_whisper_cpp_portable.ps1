param(
    [string]$VulkanSdkRoot = $env:VULKAN_SDK,
    [switch]$ForceReconfigure
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$toolsRoot = Join-Path $root "tools"
$whisperRoot = Join-Path $toolsRoot "whisper.cpp-src"
$spirvHeadersRoot = Join-Path $toolsRoot "SPIRV-Headers"
$spirvHeadersDir = Join-Path $spirvHeadersRoot "lib\cmake\SPIRV-Headers"
$buildRoot = Join-Path $whisperRoot "build-vulkan"
$outputRoot = Join-Path $root "whisper.cpp\vulkan"

$cmake = Join-Path $toolsRoot "cmake\cmake-4.3.3-windows-x86_64\bin\cmake.exe"
$ninja = Join-Path $toolsRoot "ninja\ninja.exe"
$clangBin = Join-Path $toolsRoot "llvm-mingw\llvm-mingw-20260616-msvcrt-x86_64\bin"
$cc = Join-Path $clangBin "x86_64-w64-mingw32-clang.exe"
$cxx = Join-Path $clangBin "x86_64-w64-mingw32-clang++.exe"
$rc = Join-Path $clangBin "x86_64-w64-mingw32-windres.exe"
$ccCmake = $cc -replace '\\', '/'
$cxxCmake = $cxx -replace '\\', '/'
$rcCmake = $rc -replace '\\', '/'

if (-not (Test-Path $cmake)) { throw "CMake not found: $cmake" }
if (-not (Test-Path $ninja)) { throw "Ninja not found: $ninja" }
if (-not (Test-Path $cc) -or -not (Test-Path $cxx)) { throw "LLVM-MinGW not found under $clangBin" }

if (-not $VulkanSdkRoot) {
    $candidate = Join-Path $toolsRoot "VulkanSDK"
    if (Test-Path $candidate) {
        $VulkanSdkRoot = $candidate
    } elseif (Test-Path (Join-Path $root '$sdkRoot')) {
        $VulkanSdkRoot = Join-Path $root '$sdkRoot'
    }
}

if (-not $VulkanSdkRoot -or -not (Test-Path $VulkanSdkRoot)) {
    throw "Vulkan SDK not found. Put a portable SDK under $toolsRoot\VulkanSDK or pass -VulkanSdkRoot."
}

$VulkanSdkRoot = (Resolve-Path $VulkanSdkRoot).Path
$vulkanBin = if (Test-Path (Join-Path $VulkanSdkRoot "Bin")) {
    Join-Path $VulkanSdkRoot "Bin"
} else {
    Join-Path $VulkanSdkRoot "bin"
}

$env:VULKAN_SDK = $VulkanSdkRoot
if (Test-Path $vulkanBin) {
    $env:PATH = "$vulkanBin;$clangBin;$(Split-Path $ninja);$env:PATH"
} else {
    $env:PATH = "$clangBin;$(Split-Path $ninja);$env:PATH"
}

if ($ForceReconfigure -and (Test-Path $buildRoot)) {
    Remove-Item -Recurse -Force $buildRoot
}

New-Item -ItemType Directory -Force -Path $buildRoot, $outputRoot | Out-Null

& $cmake @(
    "-S", $whisperRoot,
    "-B", $buildRoot,
    "-G", "Ninja",
    "-DGGML_VULKAN=ON",
    "-DCMAKE_BUILD_TYPE=Release",
    "-DCMAKE_C_COMPILER=$ccCmake",
    "-DCMAKE_CXX_COMPILER=$cxxCmake",
    "-DCMAKE_RC_COMPILER=$rcCmake",
    "-DSPIRV-Headers_DIR=$($spirvHeadersDir -replace '\\', '/')"
)

& $cmake "--build" $buildRoot "--config" "Release" "-j"

$builtExe = Get-ChildItem -Recurse -File -LiteralPath $buildRoot -Filter "whisper-cli.exe" | Select-Object -First 1
if (-not $builtExe) {
    throw "Build finished, but whisper-cli.exe was not found under $buildRoot"
}

$outBin = Join-Path $outputRoot "bin"
New-Item -ItemType Directory -Force -Path $outBin | Out-Null
Copy-Item -LiteralPath $builtExe.FullName -Destination (Join-Path $outBin "whisper-cli.exe") -Force
Copy-Item -LiteralPath (Join-Path $clangBin "libc++.dll") -Destination $outBin -Force
Copy-Item -LiteralPath (Join-Path $clangBin "libunwind.dll") -Destination $outBin -Force
Copy-Item -LiteralPath (Join-Path $clangBin "libomp.dll") -Destination $outBin -Force

Write-Output "Portable Vulkan build copied to: $(Join-Path $outBin 'whisper-cli.exe')"
