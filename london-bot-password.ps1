# Opens SSH to the London server (through the local proxy) and sets the bot /login password there (typed hidden).
$ssh = "C:\Program Files\Git\usr\bin\ssh.exe"
$proxy = "ProxyCommand=/mingw64/bin/connect.exe -H 127.0.0.1:10808 %h %p"
& $ssh -o $proxy -t mlops "bash ~/infra-monitor/set-bot-password.sh"
