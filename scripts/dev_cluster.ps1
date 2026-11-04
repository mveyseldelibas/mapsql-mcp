<#
Throwaway local PostGIS cluster for development (never touches other Postgres services).
Usage:  powershell -File scripts/dev_cluster.ps1 start|stop|init
Requires PostgreSQL + PostGIS binaries; set $env:PGBIN if not at the default path.
Listens on 127.0.0.1:55432, trust auth, data in ./.pgdata (git-ignored).
#>
param([Parameter(Mandatory=$true)][ValidateSet('init','start','stop','status')][string]$Action)
$root = Split-Path -Parent $PSScriptRoot
$bin  = if ($env:PGBIN) { $env:PGBIN } else { 'C:\Program Files\PostgreSQL\18\bin' }
$data = Join-Path $root '.pgdata'
switch ($Action) {
  'init' {
    & "$bin\initdb.exe" -D $data --auth=trust --username=postgres -E UTF8 --locale=C
    Add-Content "$data\postgresql.conf" "`nport = 55432`nlisten_addresses = '127.0.0.1'`nshared_buffers = 64MB`nmax_connections = 30`nunix_socket_directories = ''"
  }
  'start'  { Start-Process -WindowStyle Hidden -FilePath "$bin\pg_ctl.exe" -ArgumentList @('-D',$data,'-l',"$data\server.log",'-w','start') }
  'stop'   { & "$bin\pg_ctl.exe" -D $data -m fast stop }
  'status' { & "$bin\pg_ctl.exe" -D $data status }
}
