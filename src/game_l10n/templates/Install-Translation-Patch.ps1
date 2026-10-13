param(
    [ValidateSet("Menu", "Install", "Remove", "Verify")]
    [string]$Mode = "Menu",
    [string]$GameRoot
)

$ErrorActionPreference = "Stop"
$PackageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ManifestPath = Join-Path $PackageRoot "parr0t-patch-manifest.json"
$PayloadRoot = Join-Path $PackageRoot "payload"

function Read-PatchManifest {
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "E_MANIFEST_MISSING"
    }
    return Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Get-SafeTarget([string]$Root, [string]$Relative) {
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $relativeNative = $Relative.Replace('/', [IO.Path]::DirectorySeparatorChar)
    $candidate = [IO.Path]::GetFullPath([IO.Path]::Combine($rootFull, $relativeNative))
    $prefix = $rootFull + [IO.Path]::DirectorySeparatorChar
    if (-not $candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "E_UNSAFE_PATH"
    }
    return $candidate
}

function Add-UniqueRoot([Collections.Generic.List[string]]$Roots, [string]$Candidate) {
    if ([string]::IsNullOrWhiteSpace($Candidate)) { return }
    try {
        if (-not (Test-Path -LiteralPath $Candidate -PathType Container)) { return }
        $resolved = (Resolve-Path -LiteralPath $Candidate).Path
        if (-not ($Roots | Where-Object { $_ -ieq $resolved })) { $Roots.Add($resolved) }
    } catch { return }
}

function Get-SteamRoots {
    $roots = [Collections.Generic.List[string]]::new()
    foreach ($registryPath in @(
        "HKCU:\Software\Valve\Steam",
        "HKLM:\SOFTWARE\WOW6432Node\Valve\Steam",
        "HKLM:\SOFTWARE\Valve\Steam"
    )) {
        try {
            $item = Get-ItemProperty -LiteralPath $registryPath -ErrorAction Stop
            Add-UniqueRoot $roots $item.SteamPath
            Add-UniqueRoot $roots $item.InstallPath
        } catch { }
    }
    Add-UniqueRoot $roots "${env:ProgramFiles(x86)}\Steam"
    Add-UniqueRoot $roots "$env:ProgramFiles\Steam"

    $primaryRoots = @($roots)
    foreach ($root in $primaryRoots) {
        if (-not (Test-Path -LiteralPath $root -PathType Container)) { continue }
        $steamApps = Join-Path $root "steamapps"
        if (-not (Test-Path -LiteralPath $steamApps -PathType Container)) { continue }
        $vdf = Join-Path $steamApps "libraryfolders.vdf"
        if (-not (Test-Path -LiteralPath $vdf -PathType Leaf)) { continue }
        foreach ($line in Get-Content -LiteralPath $vdf -Encoding UTF8) {
            if ($line -match '"path"\s*"([^"]+)"') {
                Add-UniqueRoot $roots ($Matches[1].Replace('\\', '\'))
            }
        }
    }
    return @($roots)
}

function Test-GameRoot([string]$Candidate, $Manifest) {
    if ([string]::IsNullOrWhiteSpace($Candidate)) { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Container)) { return $false }
    foreach ($relative in @($Manifest.steam.required_files)) {
        $required = Get-SafeTarget $Candidate $relative
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { return $false }
    }
    return $true
}

function Find-GameRoot($Manifest) {
    $matches = [Collections.Generic.List[string]]::new()
    foreach ($root in Get-SteamRoots) {
        if (-not (Test-Path -LiteralPath $root -PathType Container)) { continue }
        $steamApps = Join-Path $root "steamapps"
        if (-not (Test-Path -LiteralPath $steamApps -PathType Container)) { continue }
        $appManifest = Join-Path $steamApps ("appmanifest_{0}.acf" -f $Manifest.steam.app_id)
        if (-not (Test-Path -LiteralPath $appManifest -PathType Leaf)) { continue }
        $installDir = [string]$Manifest.steam.install_dir
        $acf = Get-Content -LiteralPath $appManifest -Raw -Encoding UTF8
        if ($acf -match '"installdir"\s*"([^"]+)"') { $installDir = $Matches[1] }
        $common = Join-Path $steamApps "common"
        if (-not (Test-Path -LiteralPath $common -PathType Container)) { continue }
        $candidate = Join-Path $common $installDir
        if ((Test-GameRoot $candidate $Manifest) -and -not ($matches | Where-Object { $_ -ieq $candidate })) {
            $matches.Add($candidate)
        }
    }
    if ($matches.Count -eq 1) { return $matches[0] }
    return $null
}

function Resolve-GameRoot($Manifest) {
    if (Test-GameRoot $GameRoot $Manifest) { return (Resolve-Path -LiteralPath $GameRoot).Path }
    $found = Find-GameRoot $Manifest
    if ($found) { return (Resolve-Path -LiteralPath $found).Path }
    $manual = Read-Host "Steam game folder was not found. Enter the game folder"
    if (-not (Test-GameRoot $manual $Manifest)) { throw "E_GAME_ROOT_INVALID" }
    return (Resolve-Path -LiteralPath $manual).Path
}

function Get-ReceiptPath([string]$Root, $Manifest) {
    $stateRoot = Join-Path $Root ".parr0t"
    return Join-Path $stateRoot ("{0}.json" -f $Manifest.patch_id)
}

function Read-Receipt([string]$Root, $Manifest) {
    $path = Get-ReceiptPath $Root $Manifest
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    $receipt = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($receipt.patch_id -ne $Manifest.patch_id) { throw "E_RECEIPT_ID_MISMATCH" }
    return $receipt
}

function Get-Sha256([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    try {
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $hash = $sha.ComputeHash($stream) }
        finally { $sha.Dispose() }
    } finally { $stream.Dispose() }
    return ([BitConverter]::ToString($hash)).Replace("-", "").ToLowerInvariant()
}

function Test-Package($Manifest) {
    foreach ($file in @($Manifest.files)) {
        $source = Get-SafeTarget $PayloadRoot $file.path
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "E_PAYLOAD_MISSING" }
        if ((Get-Item -LiteralPath $source).Length -ne [int64]$file.bytes) { throw "E_PAYLOAD_SIZE" }
        if ((Get-Sha256 $source) -ne $file.sha256) {
            throw "E_PAYLOAD_HASH"
        }
    }
}

function Install-Patch([string]$Root, $Manifest) {
    Test-Package $Manifest
    $oldReceipt = Read-Receipt $Root $Manifest
    $owned = @{}
    if ($oldReceipt) { foreach ($file in @($oldReceipt.files)) { $owned[[string]$file.path] = $true } }
    foreach ($file in @($Manifest.files)) {
        $target = Get-SafeTarget $Root $file.path
        if ((Test-Path -LiteralPath $target) -and -not $owned.ContainsKey([string]$file.path)) {
            throw "E_UNOWNED_COLLISION"
        }
    }

    if ($oldReceipt) {
        foreach ($file in @($oldReceipt.files)) {
            $target = Get-SafeTarget $Root $file.path
            if (Test-Path -LiteralPath $target -PathType Leaf) { Remove-Item -LiteralPath $target -Force }
        }
    }

    $written = [Collections.Generic.List[string]]::new()
    try {
        foreach ($file in @($Manifest.files)) {
            $source = Get-SafeTarget $PayloadRoot $file.path
            $target = Get-SafeTarget $Root $file.path
            $parent = Split-Path -Parent $target
            New-Item -ItemType Directory -Force -Path $parent | Out-Null
            $temporary = "$target.parr0t-new"
            Copy-Item -LiteralPath $source -Destination $temporary -Force
            Move-Item -LiteralPath $temporary -Destination $target -Force
            $written.Add($target)
        }
        $receiptPath = Get-ReceiptPath $Root $Manifest
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $receiptPath) | Out-Null
        $receipt = [ordered]@{
            schema = $Manifest.schema
            patch_id = $Manifest.patch_id
            release_id = $Manifest.release_id
            files = @($Manifest.files)
        }
        $temporaryReceipt = "$receiptPath.tmp"
        $receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporaryReceipt -Encoding UTF8
        if (Test-Path -LiteralPath $receiptPath -PathType Leaf) { Remove-Item -LiteralPath $receiptPath -Force }
        Move-Item -LiteralPath $temporaryReceipt -Destination $receiptPath -Force
    } catch {
        foreach ($target in $written) { if (Test-Path -LiteralPath $target -PathType Leaf) { Remove-Item -LiteralPath $target -Force } }
        $receiptPath = Get-ReceiptPath $Root $Manifest
        if (Test-Path -LiteralPath $receiptPath -PathType Leaf) { Remove-Item -LiteralPath $receiptPath -Force }
        throw
    }
    Write-Host ("Installed {0} ({1})." -f $Manifest.display_name, $Manifest.release_id)
}

function Remove-Patch([string]$Root, $Manifest) {
    $receipt = Read-Receipt $Root $Manifest
    if (-not $receipt) { Write-Host "This patch is not installed."; return }
    foreach ($file in @($receipt.files)) {
        $target = Get-SafeTarget $Root $file.path
        if (Test-Path -LiteralPath $target -PathType Leaf) { Remove-Item -LiteralPath $target -Force }
    }
    $receiptPath = Get-ReceiptPath $Root $Manifest
    Remove-Item -LiteralPath $receiptPath -Force
    Write-Host ("Removed {0}." -f $Manifest.display_name)
}

function Verify-Patch([string]$Root, $Manifest) {
    $receipt = Read-Receipt $Root $Manifest
    if (-not $receipt) { throw "E_NOT_INSTALLED" }
    foreach ($file in @($receipt.files)) {
        $target = Get-SafeTarget $Root $file.path
        if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw "E_INSTALLED_FILE_MISSING" }
        if ((Get-Sha256 $target) -ne $file.sha256) {
            throw "E_INSTALLED_FILE_HASH"
        }
    }
    Write-Host ("Verified {0} ({1})." -f $Manifest.display_name, $receipt.release_id)
}

try {
    $manifest = Read-PatchManifest
    if ($Mode -eq "Menu") {
        Write-Host $manifest.display_name
        Write-Host "1. Install or update"
        Write-Host "2. Remove"
        Write-Host "3. Verify installed files"
        $choice = Read-Host "Choose [1]"
        if ([string]::IsNullOrWhiteSpace($choice) -or $choice -eq "1") { $Mode = "Install" }
        elseif ($choice -eq "2") { $Mode = "Remove" }
        elseif ($choice -eq "3") { $Mode = "Verify" }
        else { throw "E_MENU_CHOICE" }
    }
    $resolvedRoot = Resolve-GameRoot $manifest
    if ($Mode -eq "Install") { Install-Patch $resolvedRoot $manifest }
    elseif ($Mode -eq "Remove") { Remove-Patch $resolvedRoot $manifest }
    elseif ($Mode -eq "Verify") { Verify-Patch $resolvedRoot $manifest }
    exit 0
} catch {
    Write-Host ("[FAILED] {0}" -f $_.Exception.Message) -ForegroundColor Red
    exit 1
}
