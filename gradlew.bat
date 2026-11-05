@echo off
set JAVA_HOME=C:\Program Files\Java\jdk-27.0.1.0.3
set ANDROID_HOME=C:\Users\CADMACRO\AppData\Local\Android\Sdk
set PATH=%JAVA_HOME%\bin;%ANDROID_HOME%\platform-tools;%ANDROID_HOME%\emulator;%PATH%
cd /d D:\codes\startbot\rnapp\android
C:\Windows\System32\cmd.exe /c gradlew.bat assembleDebug --no-daemon