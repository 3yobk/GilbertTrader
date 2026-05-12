# start_all.ps1
Start-Process powershell -ArgumentList "-NoExit", "-Command", "python api_server.py"
Start-Sleep -Seconds 2
Start-Process powershell -ArgumentList "-NoExit", "-Command", "python main.py"
Start-Sleep -Seconds 2
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd pixel-perfect; npm run dev"