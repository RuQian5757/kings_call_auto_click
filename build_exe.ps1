$ErrorActionPreference = 'Stop'

# Run from this project folder. The generated executable is placed in dist\.
python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name '火焰征程背景點擊工具' `
  --add-data 'assets;assets' `
  app.py
