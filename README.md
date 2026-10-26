# tailworker

One small machine layer for private Linux compute over an existing Tailscale
network. Queues, retries, job history and application credentials stay with the
consumer. This repository is private. All container jobs run with rootless Podman.

The command provides `setup`, `ls`, `run`, `image send`, and explicit owner-only
`up gcp`/`down`. It never creates machines to satisfy a submitted job.

## Install an owned machine

The owner runs `sudo bash bootstrap.sh --owned` once on each owned Linux
machine. It installs missing Podman prerequisites, discovers NVIDIA capacity,
prepares CDI when needed and creates the shared GPU locks. It does not change
accounts, SSH, Tailscale identity or firewall policy. Existing CDI specifications
and lock-file inodes are preserved. WSL is an owned machine, never a dedicated
worker: its distributions share their network namespace.

Clone this repository on the control host, review its commit, then:

```sh
mkdir -p "$HOME/.local/bin"
ln -s "$HOME/tailworker/tailworker" "$HOME/.local/bin/tailworker"
tailworker setup
```

Setup writes `~/.config/tailworker/config.toml` and prints the SSH Include line
to add. Its `[owned]` table maps names to `"local"` for the control host or
ordinary existing SSH destinations. Keep their existing SSH configuration and
host pins. Setup accepts a blank OAuth client id while cloud setup is deferred.
Cloud credentials, when the owner later supplies them, are stored only in a
private file; all paths use their native XDG config/cache directories.

On hosts whose build filesystem is elsewhere, point `XDG_CACHE_HOME` there
before running jobs. Container inputs and outputs live beneath its tailworker
directory. The control host must not gain an inbound listener or a route to an
owned laptop; start laptop jobs on the laptop itself.

## Commands

`tailworker ls` lists every owned machine and dedicated peer tagged `tag:tw`:
address, GPU, memory bytes, GPU lock state and expiry. An unreachable machine
stays visible. Online dedicated nodes get `tw-NAME` SSH aliases only after
owner enrollment has established their host pin. Offline aliases disappear;
their existing pins remain until explicit teardown. First-contact enrollment
belongs to the cloud milestone, not discovery.

```sh
tailworker image send IMAGE --to MACHINE
tailworker run --on MACHINE --image IMAGE -- true
tailworker run --on MACHINE --gpu --image IMAGE -- nvidia-smi
tailworker run --image IMAGE --in inputs --out results -- COMMAND ARGUMENTS
```

Without `--on`, run chooses the first reachable owned machine in configuration
order, then dedicated nodes by name. `--gpu` requires a GPU. An occupied GPU
waits on its machine's shared lock; there is no scheduler. Images must already
exist; a missing image names the `image send` command to use. Image transfer
compares image IDs, then uses `save | zstd | ssh | load` when necessary. The
owner's `image send --from docker` reads an existing root Docker image with
`sudo -n docker save`; it does not run work through that daemon. An unreachable
laptop can import from the control host by starting the reverse pipe locally:
`ssh CONTROL sudo -n docker save IMAGE | podman load`.

Inputs are regular files/directories only, packed in sorted order with zero
timestamps and named by SHA-256. A cache hit sends no input again. Inputs are
mounted read-only at `/in`. `--out` must be empty or absent and receives the
job's fresh `/out`; returned links, special files and paths escaping that
directory are refused. Unused input cache entries expire after seven days.

Runs default to no network and 80% of the machine's memory. `--net` opts into
networking, subject to that machine's host policy; `--memory` overrides the cap.
The exit status is the container's. Ctrl-C stops only that run's named `tw-*`
container before returning. Jobs have no timeout and are never retried.

## Dedicated cloud workers: owner only

The owner first merges `policy.hujson` into the existing tailnet and passes its
tests. Create an OAuth client scoped to auth-key writes for the worker tags,
then run `tailworker setup` on the control host and enter its credentials at
the masked prompts. In the Google project, prepare the `tailworker` network,
subnet and outbound Cloud NAT, with no inbound firewall rules. Install and
authenticate `gcloud` and `gh` on the control host. Fill `[gcp]` in config.toml:
project, zone, region, machine type and CPU/GPU image family/project. Images
must be apt-based Ubuntu or Debian; GPU images must already include drivers.

In `[ssh]`, `public_keys` lists approved submitters' ed25519 public-key file
paths. Optional `pty_public_key` names the laptop's public key: it gets the
native `restrict,pty` authorization instead of `restrict`.

```sh
tailworker up gcp --name probe --hours 1 --spot --dry-run
tailworker up gcp --name probe --hours 1 --spot
tailworker down probe
```

`up` requires a hard lifetime of 1–24 hours and refuses while a labelled worker
exists. Local cloud operations serialize under a private lock. It refuses a
name already owned by a configured machine, tailnet node or stored host pin.
`--dry-run` prints the complete native create command and mints no credentials.
`--machine-type`, `--disk-gb` and `--gpu` select capacity; `--spot` opts into
Spot. GPU types carry their GPU and use the configured GPU image family.

Repeat `--tag tag:tw-<app>-runner` and `--allow <app>-plane:7443`
`--allow <app>-plane:8443` only for the plane that worker may reach. Names resolve
through the control host's actual tailnet status. The key is non-reusable,
ephemeral, preauthorized, tagged and valid for 15 minutes. It travels through
0600 files into instance metadata deliberately; the guest firewall blocks
metadata HTTP for unprivileged users before they exist, and first boot spends
the key. Temporary files are deleted. No secret value enters argv or images.

Startup configures a dedicated unprivileged `tw` account, rootless Podman,
tailnet-only SSH and the nft egress baseline before writing the public ready
revision. The first SSH pin is learned over the new, uniquely named tagged
tailnet node; later connections check it strictly. Stale identities, unexpected
tags and a different bootstrap revision fail. Startup resumes its persisted
tailnet identity on reboot and logs out at shutdown.

Repeat `--deploy-key OWNER/REPO` to generate private keys on the worker and
register only their public halves read-only through the owner's `gh` login.
`--deploy-dir` selects their private directory and `--deploy-base` selects the
paths consumers see inside their secret mount; for example, ci-buddy uses
`.ci-buddy/projects/<project>/secrets` and `/run/secrets`. GitHub host keys come
from its HTTPS metadata API. Private halves stay on the worker.

`down` removes only deploy keys with the exact `tw-NAME` title across the
owner's administered repositories and deletes the labelled VM. Its root
shutdown unit logs out; the job account has no Tailscale administration.
The control host must observe the named node absent before cleanup succeeds
and its alias is removed; an offline node still counts as present. It also
removes leftover deploy keys after automatic expiry. Failed cleanup names
what remains; no action is retried. VM deletion at the hard lifetime is
enforced by the provider. Creation failures leave the
possible VM for explicit owner inspection rather than guessing and deleting it.

## GPU contract

Every GPU user shares `/var/lib/tailworker/locks/gpu.want` and `gpu`. A job
locks `gpu.want`, then `gpu`, releases `gpu.want` and holds `gpu` for its GPU
work. The container inherits the locked descriptor, so it retains the lock
even if its submitting process disappears.

Resident model servers acquire `gpu` before loading models. Between jobs and
every few seconds while idle, they test `gpu.want` without waiting. A waiter
makes them unload models and release `gpu`; they reacquire it before their
next job. Health checks never wait on this lock. Containers bind-mount the
lock directory read-only; advisory flock works on read-only descriptors.

## Enroll a project

Every project uses these machines through the layer that matches its jobs:

| Work | Job layer | Project setup | Add a machine |
|---|---|---|---|
| Builds, tests, gates and sweeps | ci-buddy | Project configuration, image, fetch secrets | Install the same ci-buddy revision and add its SSH root to the coordinator |
| Durable application jobs | Its own dispatch plane | Plane node, signer/revoker, runner environment | The dispatch remote-runner kit installs and enrolls that machine |
| One-off compute | None | Nothing | `tailworker run` |

Projects create no machines or network identities themselves. They supply their
own container images, honor the GPU lock, keep credentials in their own secret
slots and state which machines may receive their data. Application runner seeds
remain on their machine. Private repositories use read-only worker deploy keys.
One dedicated machine may host several consumers at once; the GPU lock prevents
collisions while each consumer retains its own queue and credentials.

For a plane application `<app>`, dedicated runners use `tag:tw-<app>-runner`
and reach only `<app>-plane` on TCP 7443/8443. The owner merges the new app's
tag owners, grants and policy tests, including cross-app denials, and extends
the scoped OAuth client. Owned machines retain their existing identities.

## Trust and owner steps

The owner alone creates/deletes cloud machines, installs root bootstrap,
changes tailnet rules, creates OAuth clients and deploy keys, and enrolls real
production credentials. `policy.hujson` is the policy fragment to merge into
the existing tailnet; its deny tests must pass in the admin console. Grants
are additive: never weaken a test to accommodate a broad existing rule.

Dedicated machines must have a separate unprivileged account, rootless Podman,
tailnet-only OpenSSH with approved separate machine keys and strict host pins,
no Tailscale SSH, no public address, no service account and no worker-initiated
connection to submitters or private networks. Their mandatory egress baseline,
short-lived single-use tagged enrollment and hard VM lifetime are supplied by
dedicated bootstrap and `up`. A dedicated bootstrap copy must have its reviewed
commit substituted for `@REVISION@`; `up` does that automatically. A non-cloud
machine reads the same `tw-*` inputs from 0600 files beside that private copy,
outside Git.
Existing owned hosts remain trusted; containers do not prove that a compromised
host cannot attack its network. Enabling `--net` on an owned host uses that
host's current network policy.

Before production adoption the owner prepares the GPU locks, merges and tests
the tailnet policy, supplies scoped OAuth/Google/GitHub credentials outside
jobs, and performs the approved one-instance cloud check. Same-machine and
offline tests do not establish cross-machine isolation. No cloud has been
activated by fixture tests. The actual provider/network check remains an owner
operation; offline tests cannot verify those controls across real machines.

Run source checks with `python3 check.py` and `python3 -m unittest discover tests`.
The command, bootstrap and check stay below 800 lines total; tests stay below
300. These are hard caps. Their unit checks do not replace the owned-host
container, GPU, transfer and cancellation checks or the owner's cloud check.
