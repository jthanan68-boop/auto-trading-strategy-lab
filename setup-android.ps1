$env:JAVA_HOME = "C:\Program Files\Java\jdk-27.0.1.0.3"
$env:ANDROID_HOME = "C:\Users\CADMACRO\AppData\Local\Android\Sdk"
$env:PATH += ";$env:JAVA_HOME\bin"
$env:PATH += ";$env:ANDROID_HOME\platform-tools"

Write-Host "JAVA_HOME: $env:JAVA_HOME"
Write-Host "ANDROID_HOME: $env:ANDROID_HOME"
Write-Host "PATH updated successfully"