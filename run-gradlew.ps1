$env:JAVA_HOME = "C:\Program Files\Java\jdk-27.0.1.0.3"
$env:ANDROID_HOME = "C:\Users\CADMACRO\AppData\Local\Android\Sdk"
$env:PATH = "$env:JAVA_HOME\bin;$env:ANDROID_HOME\platform-tools;$env:ANDROID_HOME\emulator;$env:PATH"

Set-Location D:\codes\startbot\rnapp\android
& "D:\codes\startbot\rnapp\android\gradlew.bat" assembleDebug --no-daemon