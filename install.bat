@echo off
chcp 65001 > nul
"D:\Program Files\Anaconda\envs\study\python.exe" -m pip install -r requirements.txt -i https://pypi.org/simple
pause
