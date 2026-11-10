# Reads real CPU power / temperature / fan RPM and GPU 3D usage, prints one JSON line per 0.9s.
# Started by collector.py; exits by itself when the parent process is gone.
# Requires LibreHardwareMonitorLib.dll (shipped with MSI Afterburner / RTSS).
# Keep this file ASCII-only: Windows PowerShell 5.1 reads BOM-less files as ANSI.
# Self-test without the dashboard (prints one sample, then exits):
#   powershell -NoProfile -ExecutionPolicy Bypass -File cpu_sensor.ps1 -ParentPid 0 -Once
param(
    [Parameter(Mandatory = $true)][int]$ParentPid,
    [string]$LhmDll = '',
    [switch]$Once
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

if (-not $LhmDll) {
    $candidates = @(
        'C:\Program Files (x86)\RivaTuner Statistics Server\Plugins\Client\LHMDataProvider\LibreHardwareMonitorLib.dll',
        'C:\Program Files\RivaTuner Statistics Server\Plugins\Client\LHMDataProvider\LibreHardwareMonitorLib.dll',
        (Join-Path $PSScriptRoot 'LibreHardwareMonitorLib.dll')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { $LhmDll = $candidate; break }
    }
}

$computer = $null
if ($LhmDll -and (Test-Path -LiteralPath $LhmDll)) {
    Add-Type -Path $LhmDll
    $computer = New-Object LibreHardwareMonitor.Hardware.Computer
    $computer.IsCpuEnabled = $true
    $computer.IsMotherboardEnabled = $true
}
else {
    # No sensor library: still report CPU usage and GPU 3D load, leave watts/temp as null.
    [Console]::Error.WriteLine('LHM_MISSING')
}

$utilityCounter = New-Object System.Diagnostics.PerformanceCounter('Processor Information', '% Processor Utility', '_Total', $true)

try {
    if ($computer) { $computer.Open() }
    $null = $utilityCounter.NextValue()
    while ($Once -or (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) {
        $power = $null
        $temperature = $null
        $cpuFan = $null
        if ($computer) {
            foreach ($hardware in $computer.Hardware) {
                $hardware.Update()
                foreach ($sub in $hardware.SubHardware) { $sub.Update() }
                foreach ($item in @($hardware) + @($hardware.SubHardware)) {
                    foreach ($sensor in $item.Sensors) {
                        if ($sensor.SensorType.ToString() -eq 'Power' -and $sensor.Name -eq 'CPU Package') { $power = $sensor.Value }
                        if ($sensor.SensorType.ToString() -eq 'Temperature' -and $sensor.Name -eq 'CPU Package') { $temperature = $sensor.Value }
                        if ($sensor.SensorType.ToString() -eq 'Fan' -and $sensor.Name -eq 'CPU Fan') { $cpuFan = $sensor.Value }
                    }
                }
            }
        }

        $utility = [Math]::Min(100.0, [Math]::Max(0.0, $utilityCounter.NextValue()))

        # GPU 3D load: group every process's engtype_3D counter by engine, take the busiest engine.
        $gpuUtil = $null
        try {
            $gpuRows = Get-CimInstance -ClassName Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine -Filter "Name LIKE '%engtype_3D%'" -Property Name, UtilizationPercentage
            $engines = @{}
            foreach ($gpuRow in $gpuRows) {
                if ($gpuRow.Name -match '(luid_0x[0-9a-f]+_0x[0-9a-f]+_phys_\d+_eng_\d+)_engtype_3D') {
                    $key = $Matches[1]
                    $engines[$key] = [double]$engines[$key] + [double]$gpuRow.UtilizationPercentage
                }
            }
            if ($engines.Count -gt 0) { $gpuUtil = [Math]::Min(100.0, ($engines.Values | Measure-Object -Maximum).Maximum) }
        }
        catch { $gpuUtil = $null }

        $row = @{
            at          = [DateTimeOffset]::Now.ToUnixTimeMilliseconds() / 1000.0
            watts       = $power
            temperature = $temperature
            usage       = $utility
            gpu_3d      = $gpuUtil
            cpu_fan_rpm = $cpuFan
        }
        [Console]::Out.WriteLine(($row | ConvertTo-Json -Compress))
        [Console]::Out.Flush()
        if ($Once) { break }
        Start-Sleep -Milliseconds 900
    }
}
finally {
    if ($computer) { $computer.Close() }
    $utilityCounter.Dispose()
}
