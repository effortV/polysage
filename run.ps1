# 启动膜方 AI 界面（Streamlit）。优先用项目 .venv；没有则用当前 PATH 里的 python（如 Anaconda）。
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
& $py -c "import streamlit, jieba, trafilatura, ddgs, pymoo, docx, pypdf" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "缺少依赖，正在安装 requirements.txt ..."
    & $py -m pip install -r requirements.txt
}
& $py -m streamlit run streamlit_app.py
