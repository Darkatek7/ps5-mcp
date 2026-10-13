# Decodes a console screenshot (JPEG XR, 10-bit, as the PS5 saves them with HDR output) to an 8-bit PNG,
# scaled to MaxWidth. Uses the Windows Imaging Component, so nothing has to be installed.
param(
    [Parameter(Mandatory)] [string]$In,
    [Parameter(Mandatory)] [string]$Out,
    [int]$MaxWidth = 1280
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationCore
$stream = [IO.File]::OpenRead($In)
try {
    $decoder = [System.Windows.Media.Imaging.BitmapDecoder]::Create($stream, 'PreservePixelFormat', 'OnLoad')
    $frame = $decoder.Frames[0]
    $bitmap = New-Object System.Windows.Media.Imaging.FormatConvertedBitmap($frame, [System.Windows.Media.PixelFormats]::Bgr24, $null, 0)
    if ($MaxWidth -gt 0 -and $frame.PixelWidth -gt $MaxWidth) {
        $scale = $MaxWidth / $frame.PixelWidth
        $bitmap = New-Object System.Windows.Media.Imaging.TransformedBitmap($bitmap, (New-Object System.Windows.Media.ScaleTransform($scale, $scale)))
    }
    $encoder = New-Object System.Windows.Media.Imaging.PngBitmapEncoder
    $encoder.Frames.Add([System.Windows.Media.Imaging.BitmapFrame]::Create($bitmap))
    $output = [IO.File]::Create($Out)
    try { $encoder.Save($output) } finally { $output.Close() }
}
finally { $stream.Close() }
