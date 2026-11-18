param(
    [Parameter(Position=0)][ValidateSet('status','enqueue','stop','result')][string]$Action='status',
    [string]$Path,
    [string]$Id,
    [string]$WorkDir=(Join-Path $PSScriptRoot 'runtime\gui'),
    [ValidateRange(0,55)][int]$WaitSeconds=30
)
$ErrorActionPreference='Stop'
$taskPython='C:\Users\shuhe\AppData\Local\Programs\Python\Python312\python.exe'
$taskArguments=@((Join-Path $PSScriptRoot 'tools\gui_control.py'),$Action,'--work',$WorkDir,'--wait',"$WaitSeconds")
if($Path){$taskArguments+=@('--path',$Path)}
if($Id){$taskArguments+=@('--id',$Id)}
& $taskPython @taskArguments
exit $LASTEXITCODE
