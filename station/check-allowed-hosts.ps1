# GET-only: find the workload behind a hostname and show its Django host settings
# and recent DisallowedHost log lines. Uses only `kubectl get` and `kubectl logs`.
#
#   .\check-allowed-hosts.ps1
#   .\check-allowed-hosts.ps1 -Namespace smarted     # skip the namespace scan
param(
    [string]$HostName = 'smartedapilot.aipsharif.ir',
    [string]$Context = 'core@prod',
    [string]$Namespace,
    [string]$Since = '2h'
)

$kc = @('--context', $Context, '--request-timeout=25s')
$envPattern = 'ALLOWED|CSRF|DEBUG|HOST|DJANGO_SETTINGS'

function Get-KJson {
    $out = kubectl @kc @args -o json 2>$null
    if ($LASTEXITCODE -eq 0 -and $out) { ($out | Out-String) | ConvertFrom-Json }
}

# Print the spec of each referenced Traefik Middleware ("<ns>-<name>" refs).
# Only middlewares in the route's namespace are fetched; others are listed by name.
function Show-Middlewares([string]$ns, [string[]]$refs) {
    $mws = (Get-KJson get middlewares.traefik.io -n $ns).items
    foreach ($ref in $refs) {
        $m = $mws | Where-Object { "$($_.metadata.namespace)-$($_.metadata.name)" -eq $ref }
        if ($m) { Write-Host "   middleware ${ref}: $($m.spec | ConvertTo-Json -Depth 10 -Compress)" }
        else { Write-Host "   middleware ${ref}: (other namespace or not readable)" }
    }
}

# Find services that the hostname routes to: Ingress first, then Traefik IngressRoute.
function Find-Backends([string]$ns) {
    $svc = @()
    $ing = Get-KJson get ingress -n $ns
    foreach ($i in $ing.items) {
        foreach ($r in $i.spec.rules) {
            if ($r.host -eq $HostName) {
                $svc += $r.http.paths | ForEach-Object { $_.backend.service.name }
                Write-Host "Ingress $ns/$($i.metadata.name) -> $($svc -join ', ')"
                Write-Host "   ingressClassName: $($i.spec.ingressClassName)"
                $i.metadata.annotations.PSObject.Properties |
                    Where-Object { $_.Name -ne 'kubectl.kubernetes.io/last-applied-configuration' } |
                    ForEach-Object { Write-Host "   annotation $($_.Name)=$($_.Value)" }
                # Traefik form: "<ns>-<name>@kubernetescrd,..."
                $refs = $i.metadata.annotations.'traefik.ingress.kubernetes.io/router.middlewares'
                if ($refs) { Show-Middlewares $ns (($refs -split ',') | ForEach-Object { ($_.Trim() -split '@')[0] }) }
            }
        }
    }
    $ir = Get-KJson get ingressroutes.traefik.io -n $ns
    foreach ($i in $ir.items) {
        foreach ($r in $i.spec.routes) {
            if ($r.match -like "*$HostName*") {
                $svc += $r.services | ForEach-Object { $_.name }
                Write-Host "IngressRoute $ns/$($i.metadata.name) -> $($r.services.name -join ', ')"
                Write-Host "   match: $($r.match)"
                if ($r.middlewares) {
                    Show-Middlewares $ns ($r.middlewares | ForEach-Object {
                        $mns = if ($_.namespace) { $_.namespace } else { $ns }; "$mns-$($_.name)" })
                }
            }
        }
    }
    $svc | Where-Object { $_ } | Select-Object -Unique
}

Write-Host "context: $(kubectl config current-context) (requests use $Context)"

$namespaces = if ($Namespace) { @($Namespace) } else {
    # Likely names first so the scan usually stops early; the VPN path is slow.
    $all = (Get-KJson get namespaces).items.metadata.name
    @($all | Where-Object { $_ -match 'smart|edu' }) + @($all | Where-Object { $_ -notmatch 'smart|edu' })
}

$found = $null
foreach ($ns in $namespaces) {
    Write-Host -NoNewline "."
    $backends = Find-Backends $ns
    if ($backends) { $found = $ns; Write-Host ""; break }
}
if (-not $found) { Write-Host "`nNo Ingress/IngressRoute for $HostName in readable namespaces."; exit 1 }

foreach ($svcName in $backends) {
    $svc = Get-KJson get service $svcName -n $found
    if (-not $svc) { Write-Host "service $found/$svcName not readable"; continue }
    $selector = ($svc.spec.selector.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ','
    Write-Host "`n== service $found/$svcName  selector: $selector"

    $pods = (Get-KJson get pods -n $found -l $selector).items
    foreach ($p in $pods) {
        Write-Host "`n-- pod $($p.metadata.name)  phase=$($p.status.phase)  owner=$($p.metadata.ownerReferences[0].name)"
        foreach ($c in $p.spec.containers) {
            Write-Host "   container $($c.name)  image=$($c.image)"
            foreach ($e in $c.env | Where-Object { $_.name -match $envPattern }) {
                if ($null -ne $e.value) { Write-Host "     env $($e.name)=$($e.value)" }
                elseif ($e.valueFrom.secretKeyRef) { Write-Host "     env $($e.name) <- secret $($e.valueFrom.secretKeyRef.name)/$($e.valueFrom.secretKeyRef.key)" }
                elseif ($e.valueFrom.configMapKeyRef) { Write-Host "     env $($e.name) <- configmap $($e.valueFrom.configMapKeyRef.name)/$($e.valueFrom.configMapKeyRef.key)" }
            }
            foreach ($ef in $c.envFrom) {
                if ($ef.configMapRef) {
                    $cm = Get-KJson get configmap $ef.configMapRef.name -n $found
                    Write-Host "     envFrom configmap $($ef.configMapRef.name)"
                    if ($cm.data) {
                        $cm.data.PSObject.Properties | Where-Object { $_.Name -match $envPattern } |
                            ForEach-Object { Write-Host "       $($_.Name)=$($_.Value)" }
                    } else { Write-Host "       (not readable or empty)" }
                }
                # Secret values are never fetched; only the name is shown.
                if ($ef.secretRef) { Write-Host "     envFrom secret $($ef.secretRef.name) (values not read)" }
            }
        }

        $logs = kubectl @kc logs $p.metadata.name -n $found --all-containers --since=$Since --tail=5000 2>$null
        $hits = @($logs | Select-String -Pattern 'DisallowedHost|Invalid HTTP_HOST')
        Write-Host "   DisallowedHost lines in last ${Since}: $($hits.Count)"
        $hits | Select-Object -Last 5 | ForEach-Object { Write-Host "     $($_.Line)" }
        # uvicorn access line: INFO: 10.x.x.x:1234 - "GET /api/chat/chat/ HTTP/1.1" 400 Bad Request
        $bad = @($logs | Select-String -Pattern '" 400\b')
        Write-Host "   access-log 400 lines in last ${Since}: $($bad.Count)"
        $bad | Select-Object -Last 10 | ForEach-Object { Write-Host "     $($_.Line)" }
    }
}
