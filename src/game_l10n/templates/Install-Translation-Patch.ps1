param(
    [ValidateSet("Menu", "Install", "Restore", "Verify")]
    [string]$Mode = "Menu",
    [string]$GameRoot,
    [string]$ApprovalFile,
    [switch]$Diagnostic
)

$ErrorActionPreference = "Stop"
$InstallerPath = $MyInvocation.MyCommand.Path
$PackageRoot = Split-Path -Parent $InstallerPath
$ManifestPath = Join-Path $PackageRoot "parr0t-patch-manifest.json"
$CheckpointPath = Join-Path $PackageRoot "parr0t-release-checkpoint.json"
$PayloadRoot = Join-Path $PackageRoot "payload"
$script:InteractiveRun = $Mode -eq "Menu"
$script:MutationStarted = $false
$script:PatchManifest = $null

function Write-PatchStatus([string]$Label, [string]$Message, [ConsoleColor]$Color = [ConsoleColor]::Gray) {
    Write-Host ("[{0}] {1}" -f $Label, $Message) -ForegroundColor $Color
}

function Get-PublicFailure([string]$InternalCode) {
    if ($InternalCode -match '^E_CUSTOM_' -and $null -ne $script:PatchManifest) {
        foreach ($custom in @($script:PatchManifest.installer.custom_errors)) {
            if ([string]$custom.internal_code -ceq $InternalCode) {
                return @([string]$custom.public_code, [string]$custom.message, [string]$custom.resolution)
            }
        }
    }
    switch -Regex ($InternalCode) {
        '^E_(MANIFEST_MISSING|UNSAFE_PATH|ARTIFACT_FINGERPRINT|CHECKPOINT_MISSING|CHECKPOINT_SIZE|CHECKPOINT_HASH|PAYLOAD_MISSING|PAYLOAD_SIZE|PAYLOAD_HASH)$' {
            return @("PARR0T-E001", "설치 패키지가 손상되었거나 구성이 올바르지 않습니다.", "ZIP을 다시 다운로드한 뒤 모든 파일을 한 폴더에 압축 해제해 주세요.")
        }
        '^E_GAME_ROOT_INVALID$' {
            return @("PARR0T-E002", "올바른 게임 설치 폴더를 찾지 못했습니다.", "Steam에서 게임의 설치 폴더를 확인한 뒤 그 폴더를 다시 입력해 주세요.")
        }
        '^E_RUNTIME_APPROVAL_REQUIRED$' {
            return @("PARR0T-E005", "자동 설치에 필요한 승인이 없습니다.", "직접 설치하려면 배치 파일을 더블클릭하세요. 에이전트 자동 실행이라면 해당 작업의 일회성 승인을 전달해야 합니다.")
        }
        '^E_RUNTIME_APPROVAL_' {
            return @("PARR0T-E006", "자동 설치 승인 정보가 현재 작업과 일치하지 않습니다.", "패치 파일, 게임 폴더, 작업 종류와 유효기간을 확인한 뒤 새 승인을 발급해 주세요.")
        }
        '^E_UNOWNED_COLLISION$' {
            return @("PARR0T-E007", "패치가 관리하지 않는 동일 경로의 파일이 이미 있습니다.", "기존 모드나 패치를 확인하세요. 파일을 임의로 덮어쓰지 말고 충돌 원인을 먼저 해결해 주세요.")
        }
        '^E_NOT_INSTALLED$' {
            return @("PARR0T-E008", "설치된 패치를 찾지 못했습니다.", "먼저 패치를 설치하거나 올바른 게임 폴더를 선택해 주세요.")
        }
        '^E_(STATUS_MISSING|STATUS_INVALID|RECEIPT_MISSING|RECEIPT_ID_MISMATCH|INSTALLED_FILE_MISSING|INSTALLED_FILE_HASH)$' {
            return @("PARR0T-E009", "설치 상태 정보 또는 패치 파일이 변경되었거나 일부가 없습니다.", "설치를 다시 실행하세요. 문제가 계속되면 다른 모드와 Steam 무결성 검사 결과를 확인해 주세요.")
        }
        '^E_PATCH_ID_MISMATCH$' {
            return @("PARR0T-E010", "이 게임 폴더에는 다른 Parr0t 패치 ID가 기록되어 있습니다.", "parr0t-patch-status.txt의 패치 ID를 확인하고, 기록된 패치의 설치기로 먼저 복원해 주세요.")
        }
        '^E_(MENU_CHOICE|USER_CANCELLED)$' {
            return @("PARR0T-E011", "작업이 취소되었거나 올바른 메뉴를 선택하지 않았습니다.", "설치기를 다시 실행하고 표시된 번호나 확인 값을 입력해 주세요.")
        }
        '^E_(ORIGINAL_MISSING|ORIGINAL_BACKUP_EXISTS|ORIGINAL_BACKUP_MISSING|ORIGINAL_BACKUP_HASH|REPLACEMENT_SET_MISMATCH)$' {
            return @("PARR0T-E012", "원본 교체 파일의 설치 또는 복원 상태가 예상과 다릅니다.", "현재 파일을 임의로 이동하지 말고, 같은 패치 ID의 설치기로 상태 확인 또는 복원을 실행해 주세요.")
        }
        default {
            return @("PARR0T-E099", "설치 중 예상하지 못한 문제가 발생했습니다.", "설치기를 다시 실행해 보세요. 문제가 계속되면 진단 모드의 내부 코드와 함께 배포 페이지에 문의해 주세요.")
        }
    }
}

function Show-PublicFailure($Failure) {
    $internalCode = [string]$Failure.Exception.Message
    $public = Get-PublicFailure $internalCode
    Write-Host ""
    Write-PatchStatus "문제" ("{0} ({1})" -f $public[1], $public[0]) Red
    Write-Host ("해결 방법: {0}" -f $public[2])
    if ($script:MutationStarted) {
        Write-Host "일부 패치 파일이 변경되었을 수 있습니다. 설치기를 다시 실행해 설치 또는 상태 확인을 진행해 주세요."
    } else {
        Write-Host "게임 파일은 변경되지 않았습니다."
    }
    if ($Diagnostic) { Write-Host ("내부 진단 코드: {0}" -f $internalCode) -ForegroundColor DarkGray }
}

function Confirm-InteractiveAction([string]$Root, [string]$ActionLabel) {
    if (-not $script:InteractiveRun) { return }
    Write-Host ""
    Write-Host ("대상 게임 폴더: {0}" -f $Root)
    Write-Host ("작업: {0}" -f $ActionLabel)
    Write-Host "이 설치기는 parr0t-patch-status.txt와 패치 영수증에 기록된 파일만 관리합니다."
    $answer = Read-Host "계속하시겠습니까? [Y/N]"
    if ($answer -notmatch '(?i)^(y|yes)$' -and $answer -ne "예") { throw "E_USER_CANCELLED" }
}

if (-not ("Parr0t.NativePath" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;
namespace Parr0t {
    public static class NativePath {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern IntPtr CreateFile(string name, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern uint GetFinalPathNameByHandle(IntPtr handle, StringBuilder path, uint length, uint flags);
        [DllImport("kernel32.dll")]
        public static extern bool CloseHandle(IntPtr handle);
    }
}
"@
}

function Get-CanonicalPath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $handle = [Parr0t.NativePath]::CreateFile($full, 0, 7, [IntPtr]::Zero, 3, 0x02000000, [IntPtr]::Zero)
    if ($handle.ToInt64() -ne -1) {
        try {
            $buffer = New-Object Text.StringBuilder 32768
            $length = [Parr0t.NativePath]::GetFinalPathNameByHandle($handle, $buffer, [uint32]$buffer.Capacity, 0)
            if ($length -gt 0 -and $length -lt $buffer.Capacity) {
                return $buffer.ToString().TrimEnd([IO.Path]::DirectorySeparatorChar)
            }
        } finally {
            [Parr0t.NativePath]::CloseHandle($handle) | Out-Null
        }
    }
    return $full
}

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
    $gameCandidates = [Collections.Generic.List[string]]::new()
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
        if ((Test-GameRoot $candidate $Manifest) -and -not ($gameCandidates | Where-Object { $_ -ieq $candidate })) {
            $gameCandidates.Add($candidate)
        }
    }
    if ($gameCandidates.Count -eq 1) { return $gameCandidates[0] }
    return $null
}

function Resolve-GameRoot($Manifest) {
    if (Test-GameRoot $GameRoot $Manifest) { return (Resolve-Path -LiteralPath $GameRoot).Path }
    $found = Find-GameRoot $Manifest
    if ($found) { return (Resolve-Path -LiteralPath $found).Path }
    Write-PatchStatus "안내" "Steam에서 게임 설치 폴더를 자동으로 찾지 못했습니다." Yellow
    $manual = Read-Host "게임 설치 폴더를 입력하거나 탐색기에서 이 창으로 끌어 놓으세요"
    if (-not (Test-GameRoot $manual $Manifest)) { throw "E_GAME_ROOT_INVALID" }
    return (Resolve-Path -LiteralPath $manual).Path
}

function Get-ReceiptPath([string]$Root, $Manifest) {
    $stateRoot = Join-Path $Root ".parr0t"
    return Join-Path $stateRoot ("{0}.json" -f $Manifest.patch_id)
}

function Get-StatusPath([string]$Root, $Manifest) {
    if ([string]$Manifest.installer.status_file -cne "parr0t-patch-status.txt") { throw "E_STATUS_INVALID" }
    return Join-Path $Root "parr0t-patch-status.txt"
}

function Read-PatchStatus([string]$Root, $Manifest) {
    $statusPath = Get-StatusPath $Root $Manifest
    $receiptPath = Get-ReceiptPath $Root $Manifest
    if (-not (Test-Path -LiteralPath $statusPath -PathType Leaf)) {
        if (Test-Path -LiteralPath $receiptPath -PathType Leaf) { throw "E_STATUS_MISSING" }
        return $null
    }
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $statusPath -Encoding UTF8) {
        $parts = $line -split '=', 2
        if ($parts.Count -ne 2 -or [string]::IsNullOrWhiteSpace($parts[0])) { throw "E_STATUS_INVALID" }
        $values[$parts[0]] = $parts[1]
    }
    if ($values["contract"] -cne "parr0t.patch-status/v1" -or
        [string]::IsNullOrWhiteSpace([string]$values["patch_id"]) -or
        [string]::IsNullOrWhiteSpace([string]$values["release_id"])) {
        throw "E_STATUS_INVALID"
    }
    if ([string]$values["patch_id"] -cne [string]$Manifest.patch_id) { throw "E_PATCH_ID_MISMATCH" }
    return [pscustomobject]@{
        patch_id = [string]$values["patch_id"]
        release_id = [string]$values["release_id"]
    }
}

function Write-PatchState([string]$Root, $Manifest) {
    $statusPath = Get-StatusPath $Root $Manifest
    $temporary = "$statusPath.tmp"
    @(
        "contract=parr0t.patch-status/v1"
        "patch_id=$($Manifest.patch_id)"
        "release_id=$($Manifest.release_id)"
    ) | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $statusPath -Force
}

function Read-Receipt([string]$Root, $Manifest) {
    $status = Read-PatchStatus $Root $Manifest
    if (-not $status) { return $null }
    $path = Get-ReceiptPath $Root $Manifest
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "E_RECEIPT_MISSING" }
    $receipt = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($receipt.patch_id -ne $Manifest.patch_id) { throw "E_RECEIPT_ID_MISMATCH" }
    if ([string]$receipt.release_id -cne [string]$status.release_id) { throw "E_STATUS_INVALID" }
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

function Get-ManifestFingerprint($Manifest) {
    $lines = [Collections.Generic.List[string]]::new()
    $lines.Add("schema:$($Manifest.schema)")
    $lines.Add("patch_id:$($Manifest.patch_id)")
    $lines.Add("release_id:$($Manifest.release_id)")
    $lines.Add("display_name:$($Manifest.display_name)")
    $lines.Add("installer.contract:$($Manifest.installer.contract)")
    $lines.Add("installer.ui_locale:$($Manifest.installer.ui_locale)")
    $lines.Add("installer.status_file:$($Manifest.installer.status_file)")
    $lines.Add("installer.operations:$(@($Manifest.installer.operations) -join ',')")
    $lines.Add("steam.app_id:$($Manifest.steam.app_id)")
    $lines.Add("steam.install_dir:$($Manifest.steam.install_dir)")
    $lines.Add("checkpoint.path:$($Manifest.checkpoint.path)")
    $lines.Add("checkpoint.schema:$($Manifest.checkpoint.schema)")
    $lines.Add("checkpoint.bytes:$($Manifest.checkpoint.bytes)")
    $lines.Add("checkpoint.sha256:$($Manifest.checkpoint.sha256)")
    foreach ($custom in @($Manifest.installer.custom_errors)) {
        $lines.Add(("installer.custom:{0}`t{1}`t{2}`t{3}" -f $custom.internal_code, $custom.public_code, $custom.message, $custom.resolution))
    }
    foreach ($replacement in @($Manifest.installer.replace_files)) {
        $lines.Add(("installer.replace:{0}`t{1}" -f $replacement.path, $replacement.backup_path))
    }
    foreach ($required in @($Manifest.steam.required_files)) {
        $lines.Add("steam.required:$required")
    }
    foreach ($file in @($Manifest.files)) {
        $lines.Add(("file:{0}`t{1}`t{2}" -f $file.path, $file.bytes, $file.sha256))
    }
    $bytes = [Text.Encoding]::UTF8.GetBytes(($lines -join "`n") + "`n")
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $hash = $sha.ComputeHash($bytes) }
    finally { $sha.Dispose() }
    return ([BitConverter]::ToString($hash)).Replace("-", "").ToLowerInvariant()
}

function Use-RuntimeApproval([string]$Root, $Manifest, [string]$Action) {
    if ([string]::IsNullOrWhiteSpace($ApprovalFile)) { throw "E_RUNTIME_APPROVAL_REQUIRED" }
    if (-not (Test-Path -LiteralPath $ApprovalFile -PathType Leaf)) { throw "E_RUNTIME_APPROVAL_MISSING" }
    $approvalPath = (Resolve-Path -LiteralPath $ApprovalFile).Path
    $approvalRoot = Split-Path -Parent $approvalPath
    if ((Split-Path -Leaf $approvalRoot) -cne "runtime-approvals" -or
        (Split-Path -Leaf (Split-Path -Parent $approvalRoot)) -cne ".game-l10n") {
        throw "E_RUNTIME_APPROVAL_INVALID_PATH"
    }
    $approval = Get-Content -LiteralPath $approvalPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($approval.schema -ne "parr0t.runtime-approval/v1" -or $approval.one_shot -ne $true) {
        throw "E_RUNTIME_APPROVAL_SCHEMA"
    }
    if ($approval.authorization_source -cne "explicit_user_instruction") {
        throw "E_RUNTIME_APPROVAL_SOURCE"
    }
    if ($null -ne $approval.consumed_at_utc) { throw "E_RUNTIME_APPROVAL_CONSUMED" }
    if ([string]$approval.action -cne $Action) { throw "E_RUNTIME_APPROVAL_ACTION_MISMATCH" }
    if ($null -ne $approval.executable) { throw "E_RUNTIME_APPROVAL_EXECUTABLE_MISMATCH" }
    if ([string]$approval.artifact.fingerprint -cne [string]$Manifest.artifact_fingerprint) {
        throw "E_RUNTIME_APPROVAL_ARTIFACT_MISMATCH"
    }
    if ([string]$approval.artifact.installer_sha256 -cne (Get-Sha256 $InstallerPath)) {
        throw "E_RUNTIME_APPROVAL_INSTALLER_MISMATCH"
    }
    $approvedRootValue = [string]$approval.target
    if (Test-Path -LiteralPath $approvedRootValue -PathType Container) {
        $approvedRootValue = (Resolve-Path -LiteralPath $approvedRootValue).Path
    }
    $approvedRoot = Get-CanonicalPath $approvedRootValue
    $actualRoot = Get-CanonicalPath $Root
    if (-not $approvedRoot.Equals($actualRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "E_RUNTIME_APPROVAL_TARGET_MISMATCH"
    }
    try { $expires = [DateTimeOffset]::Parse([string]$approval.expires_at_utc) }
    catch { throw "E_RUNTIME_APPROVAL_EXPIRY_INVALID" }
    if ($expires -le [DateTimeOffset]::UtcNow) { throw "E_RUNTIME_APPROVAL_EXPIRED" }

    $approval.consumed_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
    $temporary = "$approvalPath.tmp"
    $approval | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $approvalPath -Force
}

function Test-Package($Manifest) {
    if ((Get-ManifestFingerprint $Manifest) -ne $Manifest.artifact_fingerprint) {
        throw "E_ARTIFACT_FINGERPRINT"
    }
    if (-not (Test-Path -LiteralPath $CheckpointPath -PathType Leaf)) { throw "E_CHECKPOINT_MISSING" }
    if ((Get-Item -LiteralPath $CheckpointPath).Length -ne [int64]$Manifest.checkpoint.bytes) {
        throw "E_CHECKPOINT_SIZE"
    }
    if ((Get-Sha256 $CheckpointPath) -ne [string]$Manifest.checkpoint.sha256) {
        throw "E_CHECKPOINT_HASH"
    }
    foreach ($file in @($Manifest.files)) {
        $source = Get-SafeTarget $PayloadRoot $file.path
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "E_PAYLOAD_MISSING" }
        if ((Get-Item -LiteralPath $source).Length -ne [int64]$file.bytes) { throw "E_PAYLOAD_SIZE" }
        if ((Get-Sha256 $source) -ne $file.sha256) {
            throw "E_PAYLOAD_HASH"
        }
    }
}

function Get-ReplacementMap($Manifest) {
    $result = @{}
    foreach ($replacement in @($Manifest.installer.replace_files)) {
        $result[[string]$replacement.path] = [string]$replacement.backup_path
    }
    return $result
}

function Assert-OwnedState([string]$Root, $Receipt) {
    foreach ($file in @($Receipt.files)) {
        $target = Get-SafeTarget $Root $file.path
        if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw "E_INSTALLED_FILE_MISSING" }
        if ((Get-Sha256 $target) -ne $file.sha256) { throw "E_INSTALLED_FILE_HASH" }
    }
    foreach ($original in @($Receipt.originals)) {
        $backup = Get-SafeTarget $Root $original.backup_path
        if (-not (Test-Path -LiteralPath $backup -PathType Leaf)) { throw "E_ORIGINAL_BACKUP_MISSING" }
        if ((Get-Item -LiteralPath $backup).Length -ne [int64]$original.bytes -or
            (Get-Sha256 $backup) -ne $original.sha256) {
            throw "E_ORIGINAL_BACKUP_HASH"
        }
    }
}

function Install-Patch([string]$Root, $Manifest) {
    Write-PatchStatus "확인" "설치 패키지의 무결성을 확인하고 있습니다."
    Test-Package $Manifest
    $oldReceipt = Read-Receipt $Root $Manifest
    $runtimeAction = if ($oldReceipt) { "update" } else { "install" }
    $replacementMap = Get-ReplacementMap $Manifest
    $owned = @{}
    if ($oldReceipt) {
        Assert-OwnedState $Root $oldReceipt
        foreach ($file in @($oldReceipt.files)) { $owned[[string]$file.path] = $true }
        $oldReplacementPaths = @($oldReceipt.originals | ForEach-Object { [string]$_.path } | Sort-Object)
        $newReplacementPaths = @($Manifest.installer.replace_files | ForEach-Object { [string]$_.path } | Sort-Object)
        if (($oldReplacementPaths -join "`n") -cne ($newReplacementPaths -join "`n")) {
            throw "E_REPLACEMENT_SET_MISMATCH"
        }
    }
    foreach ($file in @($Manifest.files)) {
        $target = Get-SafeTarget $Root $file.path
        $isReplacement = $replacementMap.ContainsKey([string]$file.path)
        if (-not $oldReceipt -and $isReplacement -and -not (Test-Path -LiteralPath $target -PathType Leaf)) {
            throw "E_ORIGINAL_MISSING"
        }
        if ((Test-Path -LiteralPath $target) -and -not $owned.ContainsKey([string]$file.path) -and -not $isReplacement) {
            throw "E_UNOWNED_COLLISION"
        }
        if (-not $oldReceipt -and $isReplacement) {
            $backup = Get-SafeTarget $Root $replacementMap[[string]$file.path]
            if (Test-Path -LiteralPath $backup) { throw "E_ORIGINAL_BACKUP_EXISTS" }
        }
    }

    if ($script:InteractiveRun) {
        Confirm-InteractiveAction $Root $(if ($oldReceipt) { "패치 업데이트" } else { "패치 설치" })
    } else {
        Use-RuntimeApproval $Root $Manifest $runtimeAction
    }
    $script:MutationStarted = $true
    Write-PatchStatus "진행" $(if ($oldReceipt) { "패치를 업데이트하고 있습니다." } else { "패치를 설치하고 있습니다." }) Cyan

    $written = [Collections.Generic.List[string]]::new()
    $originals = [Collections.Generic.List[object]]::new()
    try {
        if ($oldReceipt) {
            foreach ($original in @($oldReceipt.originals)) { $originals.Add($original) }
            foreach ($file in @($oldReceipt.files)) {
                $target = Get-SafeTarget $Root $file.path
                if (Test-Path -LiteralPath $target -PathType Leaf) { Remove-Item -LiteralPath $target -Force }
            }
        } else {
            foreach ($replacement in @($Manifest.installer.replace_files)) {
                $target = Get-SafeTarget $Root $replacement.path
                $backup = Get-SafeTarget $Root $replacement.backup_path
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $backup) | Out-Null
                $temporaryBackup = "$backup.parr0t-new"
                Copy-Item -LiteralPath $target -Destination $temporaryBackup
                Move-Item -LiteralPath $temporaryBackup -Destination $backup
                $originals.Add([ordered]@{
                    path = [string]$replacement.path
                    backup_path = [string]$replacement.backup_path
                    bytes = (Get-Item -LiteralPath $backup).Length
                    sha256 = Get-Sha256 $backup
                })
            }
        }
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
            originals = @($originals)
        }
        $temporaryReceipt = "$receiptPath.tmp"
        $receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporaryReceipt -Encoding UTF8
        if (Test-Path -LiteralPath $receiptPath -PathType Leaf) { Remove-Item -LiteralPath $receiptPath -Force }
        Move-Item -LiteralPath $temporaryReceipt -Destination $receiptPath -Force
        Write-PatchState $Root $Manifest
    } catch {
        foreach ($target in $written) { if (Test-Path -LiteralPath $target -PathType Leaf) { Remove-Item -LiteralPath $target -Force } }
        foreach ($original in @($originals)) {
            try {
                $backup = Get-SafeTarget $Root $original.backup_path
                $target = Get-SafeTarget $Root $original.path
                if (Test-Path -LiteralPath $backup -PathType Leaf) {
                    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
                    Copy-Item -LiteralPath $backup -Destination $target -Force
                    Remove-Item -LiteralPath $backup -Force
                }
            } catch { }
        }
        $receiptPath = Get-ReceiptPath $Root $Manifest
        if (Test-Path -LiteralPath $receiptPath -PathType Leaf) { Remove-Item -LiteralPath $receiptPath -Force }
        $statusPath = Get-StatusPath $Root $Manifest
        if (Test-Path -LiteralPath $statusPath -PathType Leaf) { Remove-Item -LiteralPath $statusPath -Force }
        throw
    }
    Write-PatchStatus "완료" $(if ($oldReceipt) {
        "패치 업데이트가 완료되었습니다. ($($Manifest.release_id)) [PARR0T-S002]"
    } else {
        "패치 설치가 완료되었습니다. ($($Manifest.release_id)) [PARR0T-S001]"
    }) Green
}

function Restore-Patch([string]$Root, $Manifest) {
    $receipt = Read-Receipt $Root $Manifest
    if (-not $receipt) { Write-PatchStatus "안내" "설치된 패치를 찾지 못했습니다. 변경 사항이 없습니다." Yellow; return }
    Assert-OwnedState $Root $receipt
    if ($script:InteractiveRun) { Confirm-InteractiveAction $Root "패치 복원" }
    else { Use-RuntimeApproval $Root $Manifest "remove" }
    $script:MutationStarted = $true
    Write-PatchStatus "진행" "패치 파일을 제거하여 설치 전 상태로 복원하고 있습니다." Cyan
    $originalMap = @{}
    foreach ($original in @($receipt.originals)) { $originalMap[[string]$original.path] = $original }
    foreach ($file in @($receipt.files)) {
        $target = Get-SafeTarget $Root $file.path
        if ($originalMap.ContainsKey([string]$file.path)) {
            $original = $originalMap[[string]$file.path]
            $backup = Get-SafeTarget $Root $original.backup_path
            $temporary = "$target.parr0t-restore"
            Copy-Item -LiteralPath $backup -Destination $temporary -Force
            Move-Item -LiteralPath $temporary -Destination $target -Force
            Remove-Item -LiteralPath $backup -Force
        } elseif (Test-Path -LiteralPath $target -PathType Leaf) {
            Remove-Item -LiteralPath $target -Force
        }
    }
    $receiptPath = Get-ReceiptPath $Root $Manifest
    Remove-Item -LiteralPath $receiptPath -Force
    $statusPath = Get-StatusPath $Root $Manifest
    Remove-Item -LiteralPath $statusPath -Force
    Write-PatchStatus "완료" "패치 복원이 완료되었습니다. [PARR0T-S003]" Green
}

function Verify-Patch([string]$Root, $Manifest) {
    $receipt = Read-Receipt $Root $Manifest
    if (-not $receipt) { throw "E_NOT_INSTALLED" }
    Assert-OwnedState $Root $receipt
    Write-PatchStatus "완료" ("설치된 패치 파일이 정상입니다. ({0}) [PARR0T-S004]" -f $receipt.release_id) Green
}

try {
    $manifest = Read-PatchManifest
    $script:PatchManifest = $manifest
    if ($Mode -eq "Menu") {
        Write-Host ""
        Write-Host ("{0} 설치 도우미" -f $manifest.display_name) -ForegroundColor Cyan
        Write-Host ("배포 버전: {0}" -f $manifest.release_id)
        Write-Host ""
        Write-Host "1. 설치 또는 업데이트"
        Write-Host "2. 복원"
        Write-Host "3. 설치 상태 확인"
        Write-Host "0. 종료"
        $choice = Read-Host "선택 [1]"
        if ([string]::IsNullOrWhiteSpace($choice) -or $choice -eq "1") { $Mode = "Install" }
        elseif ($choice -eq "2") { $Mode = "Restore" }
        elseif ($choice -eq "3") { $Mode = "Verify" }
        elseif ($choice -eq "0") { exit 0 }
        else { throw "E_MENU_CHOICE" }
    }
    Write-PatchStatus "확인" "게임 설치 위치를 확인하고 있습니다."
    $resolvedRoot = Resolve-GameRoot $manifest
    if ($Mode -eq "Install") { Install-Patch $resolvedRoot $manifest }
    elseif ($Mode -eq "Restore") { Restore-Patch $resolvedRoot $manifest }
    elseif ($Mode -eq "Verify") { Verify-Patch $resolvedRoot $manifest }
    exit 0
} catch {
    Show-PublicFailure $_
    exit 1
}
