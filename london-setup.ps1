# Opens SSH to the London server (through the local proxy) and runs the receiver setup there.
# You type the bot token when asked; it is read hidden on the server and never passes through this window's history.
$ssh = "C:\Program Files\Git\usr\bin\ssh.exe"
$proxy = "ProxyCommand=/mingw64/bin/connect.exe -H 127.0.0.1:10808 %h %p"
& $ssh -o $proxy -t mlops "rm -f ~/infra-monitor/state.json && bash ~/infra-monitor/setup.sh"
