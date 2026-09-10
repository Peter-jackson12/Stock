# setup_kiwoom.ps1 (새 컴퓨터에서 32비트 환경 자동 구축 스크립트)
Write-Host "⚙️ 키움 전용 32비트 파이썬 가상환경(.venv32)을 자동 생성합니다..." -ForegroundColor Cyan

uv python install cpython-3.10.11-windows-i686-none
uv venv .venv32 --python cpython-3.10.11-windows-i686-none
uv pip install -r collector/kiwoom/requirements_32.txt --python .venv32

Write-Host "✅ .venv32 세팅 완료! 이제 키움 수집기를 바로 실행할 수 있습니다." -ForegroundColor Green