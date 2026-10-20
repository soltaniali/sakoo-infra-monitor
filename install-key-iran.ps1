# Installs Claude's deploy SSH key on the Iran probe host. You type the server password once; it is not stored.
$ssh = "C:\Program Files\Git\usr\bin\ssh.exe"
$key = Get-Content "$env:USERPROFILE\.ssh\id_ed25519_mlops.pub"
$remote = "umask 077; mkdir -p ~/.ssh; grep -qxF '$key' ~/.ssh/authorized_keys 2>/dev/null || echo '$key' >> ~/.ssh/authorized_keys; echo key-installed"
& $ssh -o StrictHostKeyChecking=accept-new ali_soltani@213.233.184.128 $remote
