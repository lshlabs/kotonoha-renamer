param([Parameter(Mandatory=$true)][string]$InstallerPath)
$ErrorActionPreference = 'Stop'
$signature = Get-AuthenticodeSignature -LiteralPath $InstallerPath
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch '(^|,\s*)O=Ollama Inc\.(,|$)') {
    throw 'The downloaded installer does not have a valid Ollama Inc. signature.'
}
Write-Output $signature.SignerCertificate.Subject
