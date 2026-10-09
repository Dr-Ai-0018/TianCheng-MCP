# Fixed argv forwarding avoids assembling user paths into shell command text.
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$program=$args[0]
$forward=@()
if ($args.Count -gt 1) { $forward=@($args[1..($args.Count-1)]) }
& $program @forward
if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }
