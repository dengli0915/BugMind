# BugMind

BugMind 是一个编程学习平台，提供分级错题诊断、变式练习、作业管理和班级学情分析。

- frontend/ 包含单文件前端。
- backend/ 包含 FastAPI 后端、环境变量模板和接口文档。

## 本地运行

1. 在 backend/ 中建立 Python 虚拟环境，安装 requirements.txt 中的依赖。
2. 如需大模型诊断，将 backend/.env.example 复制为 backend/.env，填写自己的 DeepSeek API 配置。
3. 在 backend/ 中运行 python main.py，再在浏览器中打开 frontend/ 中的 HTML 文件。本地前端默认连接 127.0.0.1:8000。
4. 首次使用时，在页面上注册账号。此副本从空白账号和练习记录开始。

运行产生的数据、上传附件和本地密钥由 Git 忽略。
