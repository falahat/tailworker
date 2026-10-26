#!/bin/bash
# Only the owner runs bootstrap. Owned machines keep their identity and network.
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'bootstrap requires the owner to run it as root' >&2; exit 2; }
[[ $# == 1 && $1 == --owned ]] || { echo 'usage: bash bootstrap.sh --owned' >&2; exit 2; }
export DEBIAN_FRONTEND=noninteractive
missing=()
for pair in podman:podman newuidmap:uidmap pasta:passt zstd:zstd python3:python3 flock:util-linux; do
    command -v "${pair%%:*}" >/dev/null || missing+=("${pair#*:}")
done
if ((${#missing[@]})); then apt-get update; apt-get install -y "${missing[@]}"; fi
if command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null; then
    if ! command -v nvidia-ctk >/dev/null; then
        apt-get update; apt-get install -y curl ca-certificates gpg
        curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
        curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list |
            sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' > /etc/apt/sources.list.d/nvidia-container-toolkit.list
        apt-get update; apt-get install -y nvidia-container-toolkit
    fi
    if [[ ! -f /etc/cdi/nvidia.yaml && ! -f /var/run/cdi/nvidia.yaml ]]; then
        install -d -m 755 /etc/cdi
        mode=(); [[ $(</proc/version) != *Microsoft* ]] || mode=(--mode=wsl)
        nvidia-ctk cdi generate "${mode[@]}" --output=/etc/cdi/nvidia.yaml
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
             gpu_name=gpu, gpu_memory_bytes=gpu_memory, expires_at='')
path = pathlib.Path('/var/lib/tailworker/capacity.json')
path.write_text(json.dumps(value) + '\n')
path.chmod(0o644)
PY
echo 'Owned machine prepared; user, SSH, firewall and tailnet settings unchanged.'
