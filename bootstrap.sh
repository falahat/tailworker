#!/bin/bash
# Only the owner runs bootstrap. Owned machines keep their identity and network.
set -euo pipefail
set +x
umask 077
[[ $(id -u) == 0 ]] || { echo 'bootstrap requires the owner to run it as root' >&2; exit 2; }
owned=false; [[ $# != 1 || $1 != --owned ]] || owned=true
[[ $# == 0 || $owned == true ]] || { echo 'usage: bash bootstrap.sh [--owned]' >&2; exit 2; }
if [[ $owned == false && $(</proc/version) == *[Mm]icrosoft* ]]; then echo 'dedicated bootstrap refuses WSL' >&2; exit 2; fi
export DEBIAN_FRONTEND=noninteractive
missing=()
for pair in podman:podman newuidmap:uidmap pasta:passt zstd:zstd python3:python3 flock:util-linux; do
    command -v "${pair%%:*}" >/dev/null || missing+=("${pair#*:}")
done
[[ $owned == true ]] || missing+=(nftables openssh-server git curl ca-certificates)
if ((${#missing[@]})); then apt-get update; apt-get install -y "${missing[@]}"; fi
if [[ $owned == false ]]; then
    revision='@REVISION@'
    [[ $revision =~ ^[0-9a-f]{40}$ ]] || { echo 'dedicated bootstrap needs its reviewed commit substituted' >&2; exit 2; }
    install -d -m 755 /var/lib/tailworker
    rm -f /var/lib/tailworker/ready
    meta() {
        if [[ -f $(dirname "$0")/$1 ]]; then cat "$(dirname "$0")/$1"
        else curl -fsS -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"; fi
    }
    name=$(meta tw-name)
    tags=$(meta tw-tags)
    expires=$(meta tw-expires-at)
    allow=$(meta tw-allow)
    allow_rules=$(python3 - "$allow" <<'PY'
import ipaddress, sys
for value in sys.argv[1].split():
    address, port = value.rsplit(':', 1)
    address = ipaddress.ip_address(address)
    port = int(port)
    assert 0 < port < 65536
    print(f'        {"ip" if address.version == 4 else "ip6"} daddr {address} tcp dport {port} accept')
PY
)
    cat >/etc/nftables.conf <<NFT
destroy table inet tailworker
table inet tailworker {
    chain input {
        type filter hook input priority -20; policy accept;
        ct state established,related accept
        iifname "lo" accept
        iifname "tailscale0" tcp dport 22 accept
        tcp dport 22 drop
    }
    chain output {
        type filter hook output priority -20; policy accept;
        meta skuid 0 accept
        ct state established,related accept
$allow_rules
        ip daddr 169.254.169.254 tcp dport 80 drop
        ip daddr 169.254.169.254 accept
        ip daddr { 169.254.0.0/16, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10 } drop
        ip6 daddr { fc00::/7, fe80::/10 } drop
    }
}
NFT
    # Establish the boundary before any unprivileged account or job exists.
    nft -f /etc/nftables.conf
    systemctl enable nftables.service
    source /etc/os-release
    [[ $ID == ubuntu || $ID == debian ]] || { echo 'dedicated bootstrap requires an apt Linux image' >&2; exit 2; }
    curl -fsSLo /usr/share/keyrings/tailscale-archive-keyring.gpg "https://pkgs.tailscale.com/stable/$ID/$VERSION_CODENAME.noarmor.gpg"
    chmod 644 /usr/share/keyrings/tailscale-archive-keyring.gpg
    curl -fsSLo /etc/apt/sources.list.d/tailscale.list "https://pkgs.tailscale.com/stable/$ID/$VERSION_CODENAME.tailscale-keyring.list"
    chmod 644 /etc/apt/sources.list.d/tailscale.list
    apt-get update; apt-get install -y tailscale
    id tw >/dev/null 2>&1 || useradd --create-home --user-group --shell /bin/bash tw
    loginctl enable-linger tw
    install -d -m 700 -o tw -g tw /home/tw/.ssh
    meta tw-authorized-keys >/home/tw/.ssh/authorized_keys
    chmod 600 /home/tw/.ssh/authorized_keys; chown tw:tw /home/tw/.ssh/authorized_keys
    cat >/etc/ssh/sshd_config <<'SSH'
Port 22
AllowUsers tw
AuthenticationMethods publickey
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
DisableForwarding yes
PermitUserRC no
PermitUserEnvironment no
UsePAM yes
SSH
    ssh-keygen -A
    systemctl disable --now ssh.socket
    # nft already restricts SSH to tailscale0 while its address is being assigned.
    systemctl enable --now ssh.service tailscaled.service
    systemctl restart ssh.service
    install -d -m 700 /run/tailworker
    join=/run/tailworker/authkey
    trap 'rm -f "$join"' EXIT
    if ! tailscale status --json | python3 -c 'import json,sys; sys.exit(json.load(sys.stdin)["BackendState"]!="Running")'; then
        meta tw-authkey >"$join"
        tailscale up --auth-key="file:$join" --hostname="$name" --advertise-tags="${tags// /,}" --accept-dns=false --accept-routes=false --ssh=false
        rm -f "$join"
    fi
    printf 'ListenAddress %s\n' "$(tailscale ip -4)" >>/etc/ssh/sshd_config
    if ipv6=$(tailscale ip -6); then printf 'ListenAddress %s\n' "$ipv6" >>/etc/ssh/sshd_config; fi
    sshd -t; systemctl reload ssh.service
    cat >/etc/systemd/system/tailworker-logout.service <<'UNIT'
[Unit]
After=tailscaled.service network-online.target
[Service]
Type=oneshot
ExecStart=/bin/true
RemainAfterExit=yes
ExecStop=/usr/bin/tailscale logout
[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload; systemctl enable --now tailworker-logout.service
    export TW_EXPIRES_AT=$expires
fi
if command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null; then
    if ! command -v nvidia-ctk >/dev/null; then
        apt-get update; apt-get install -y curl ca-certificates gpg
        curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --batch --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
        curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list |
            sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' > /etc/apt/sources.list.d/nvidia-container-toolkit.list
        chmod 644 /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg /etc/apt/sources.list.d/nvidia-container-toolkit.list
        apt-get update; apt-get install -y nvidia-container-toolkit
    fi
    if [[ ! -f /etc/cdi/nvidia.yaml && ! -f /var/run/cdi/nvidia.yaml ]]; then
        install -d -m 755 /etc/cdi
        mode=(); [[ $(</proc/version) != *Microsoft* ]] || mode=(--mode=wsl)
        nvidia-ctk cdi generate "${mode[@]}" --output=/etc/cdi/nvidia.yaml
        chmod 644 /etc/cdi/nvidia.yaml
    fi
fi
for path in /var/lib/tailworker /var/lib/tailworker/locks /var/lib/tailworker/locks/gpu /var/lib/tailworker/locks/gpu.want; do
    [[ ! -L $path ]] || { echo 'refusing a symlink in the lock directory' >&2; exit 2; }
done
install -d -m 755 /var/lib/tailworker/locks
# Never replace an inode: existing holders and waiters must retain one lock.
touch /var/lib/tailworker/locks/gpu /var/lib/tailworker/locks/gpu.want
chmod 666 /var/lib/tailworker/locks/gpu /var/lib/tailworker/locks/gpu.want
python3 - <<'PY'
import json, os, pathlib, subprocess
gpu, gpu_memory = '', 0
try:
    rows = subprocess.check_output(['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader,nounits'], text=True).strip().splitlines()
    gpu = '; '.join(row.rsplit(', ', 1)[0] for row in rows)
    gpu_memory = sum(int(row.rsplit(', ', 1)[1]) for row in rows) * 1024**2
except FileNotFoundError:
    pass
value = dict(cores=os.cpu_count(), memory_bytes=os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES'),
             gpu_name=gpu, gpu_memory_bytes=gpu_memory, expires_at=os.environ.get('TW_EXPIRES_AT', ''))
path = pathlib.Path('/var/lib/tailworker/capacity.json')
path.write_text(json.dumps(value) + '\n')
path.chmod(0o644)
PY
if [[ $owned == false ]]; then printf '%s\n' "$revision" >/var/lib/tailworker/ready; chmod 644 /var/lib/tailworker/ready
else echo 'Owned machine prepared; user, SSH, firewall and tailnet settings unchanged.'; fi
