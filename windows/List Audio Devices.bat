@echo off
title Coastal Radio - audio devices
cd /d "%~dp0"
CoastalRadio.exe --list-audio
echo.
echo Use the name or number of each radio's audio as "audio_device" in config.json.
echo.
pause
