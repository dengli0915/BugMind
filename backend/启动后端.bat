@echo off
setlocal EnableExtensions
title BugMind 后端服务 (127.0.0.1:8000)
color 0B

cd /d "%~dp0"

echo ============================================
echo    BugMind 后端服务  FastAPI + DeepSeek
echo ============================================
echo.

if not exist ".env" if not exist "..\.env" (
  echo [提醒] 没有找到 .env
  echo        请把 .env.example 复制为 .env，并填写 DEEPSEEK_API_KEY
  echo        可放在本目录，或项目根目录（与 前端 同级）
  echo.
)

if not exist ".venv\Scripts\python.exe" (
  echo [首次运行] 正在创建虚拟环境...
  python -m venv .venv
  if errorlevel 1 (
    echo [错误] 创建虚拟环境失败，请确认已安装 Python 3.9 以上版本
    echo        可尝试：python -m venv .venv
    goto :fail
  )
)

".venv\Scripts\python.exe" -c "import fastapi, uvicorn, httpx, dotenv" >nul 2>nul
if errorlevel 1 (
  echo [依赖] 正在安装依赖，首次可能要 1-2 分钟...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重试
    goto :fail
  )
)

echo [启动] 服务地址：http://127.0.0.1:8000
echo        接口文档：http://127.0.0.1:8000/docs
echo        按 Ctrl+C 可停止服务
echo.
set "PYTHONIOENCODING=gbk"
".venv\Scripts\python.exe" main.py

echo.
echo [已停止] 服务已退出

:fail
if /i not "%~1"=="--no-pause" pause
endlocal
exit /b 0
