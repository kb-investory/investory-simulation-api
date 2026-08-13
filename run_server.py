"""
================================================================================
[Investory Server Launcher] run_server.py
================================================================================
■ 역할:
  - Uvicorn ASGI 서버를 통해 Investory 통합 백엔드 애플리케이션(app.main:app)을 실행합니다.
  - 실행 명령어: python run_server.py
================================================================================
"""

import sys
import os
import uvicorn

# 프로젝트 루트 경로를 sys.path에 추가
root_dir = os.path.dirname(os.path.abspath(__file__))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from app.config import settings

if __name__ == "__main__":
    print(f"==================================================")
    print(f"Starting {settings.PROJECT_NAME} v{settings.VERSION}")
    print(f"URL: http://localhost:{settings.PORT}")
    print(f"Swagger API Docs: http://localhost:{settings.PORT}/docs")
    print(f"==================================================")
    
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.PORT,
        reload=True
    )
