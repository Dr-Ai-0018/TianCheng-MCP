param([ValidateSet('tunnel','supervisor')][string]$Kind)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$filter=if ($Kind -eq 'tunnel') { "Name = 'tunnel-client.exe'" } else { "Name = 'python.exe' OR Name = 'pythonw.exe'" }
$records=@(Get-CimInstance Win32_Process -Filter $filter |
    Select-Object Name,ExecutablePath,CommandLine,ProcessId)
ConvertTo-Json -InputObject $records -Depth 3 -Compress
