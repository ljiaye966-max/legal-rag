@echo off
chcp 65001 > nul
"D:\Program Files\Anaconda\envs\study\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8001
pause
