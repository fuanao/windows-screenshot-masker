@echo off
setlocal
cd /d "%~dp0"
py -m pip install -r requirements.txt
py screenshot_masker.py
