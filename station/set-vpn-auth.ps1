# Writes secrets\cluster-auth.txt (VPN username + password) with LF line endings, password typed hidden.
$u = Read-Host "VPN username [se-user15]"; if (-not $u) { $u = "se-user15" }
$s = Read-Host "VPN password" -AsSecureString
$p = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
[IO.File]::WriteAllText("$PSScriptRoot\secrets\cluster-auth.txt", "$u`n$p`n")
"written: $PSScriptRoot\secrets\cluster-auth.txt"
