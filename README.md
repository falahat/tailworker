# tailworker

Shared private compute: Linux machines over Tailscale and native Google Cloud
Run Jobs and Services. Projects own their workloads and queues; tailworker owns
resource lifecycle and discovery. Linux jobs use rootless Podman; Cloud Run
provides its own runtime. This repository is private.

Commands: `setup`, `ls`, `run`, `image send`, owner-only `up gcp`/`down`, and
`job`/`service` operations. Execution never creates a resource automatically.

## Install an owned machine

The owner runs `sudo bash bootstrap.sh --owned` once on each owned Linux
machine. It installs missing Podman prerequisites, discovers NVIDIA capacity,
prepares CDI when needed and creates the shared GPU locks. It does not change
accounts, SSH, Tailscale identity or firewall policy. Existing CDI specifications
and lock-file inodes are preserved. WSL is an owned machine, never a dedicated
worker: its distributions share their network namespace.

Clone this repository's `main` branch on the control host, review its commit, then:

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
`ls --json` returns logical names, SSH aliases, availability and capacity.
Use the logical name with `--on`; use its `host` alias with SSH.

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

## Cloud Run

Cloud Run commands need Python 3.11+ and authenticated `gcloud`, without Podman,
Tailscale or Linux bootstrap on the caller. Windows can use `python tailworker
service ...`. The owner prepares the project/APIs, container images, dedicated
workload and caller service accounts, VPC/subnet, egress firewall and private
route. Add actual values to the owner's XDG `tailworker/config.toml`:

```toml
[gcp]
project = "your-google-project"
region = "us-central1"

[cloud_run]
service_account = "worker@your-google-project.iam.gserviceaccount.com"
network = "tailworker"
subnet = "tailworker"
invokers = ["serviceAccount:caller@your-google-project.iam.gserviceaccount.com"]
invoke_as = "caller@your-google-project.iam.gserviceaccount.com"
```

Workload identity gets only its data permissions. The deployer needs resource/IAM
administration; callers need resource/IAM read access plus execution permission.
`ls` needs project-level list access. Service invocation impersonates `invoke_as`
with an audience-bound identity token; the control identity needs token-creator
permission on that approved caller account. No service-account keys, GitHub
fetch tokens or control credentials are sent to workloads.

The owner reviews a local dry run before deployment. These sample images exist:

```sh
tailworker service deploy hello --image us-docker.pkg.dev/cloudrun/container/hello --dry-run
tailworker service deploy hello --image us-docker.pkg.dev/cloudrun/container/hello
tailworker service describe hello
tailworker service invoke hello

tailworker job deploy sample --image us-docker.pkg.dev/cloudrun/container/job --dry-run
tailworker job deploy sample --image us-docker.pkg.dev/cloudrun/container/job
tailworker job run sample --wait
tailworker job executions sample
```

Both kinds support `ls`, `describe NAME`, `logs NAME` and `delete NAME`.
`job cancel NAME EXECUTION` checks the execution belongs to that job. Without
`--wait`, run returns its execution ID immediately. Waiting returns nonzero on
failure. Submission uses one HTTPS POST; it is never retried. After a disconnect,
inspect execution history before submitting again. Google task/request deadlines
remain platform limits; tailworker adds no watchdog. SDK reads and idempotent
resource operations retain Google's transport behavior.

All output is JSON. Discovery returns `kind`, logical `name`, provider `id` and
readiness; Services include `url`, token `audience`, authentication and ingress.
Google's records are authoritative, with a `tw-` prefix and `tailworker=1` label.
Callers keep project/region/name and resolve again instead of storing an IP.
No registry, discovery daemon or resource database is added. Replacement and
deletion refuse unowned resources; failures may leave a resource for inspection.

Services enforce internal ingress, IAM, zero minimum and one maximum instance.
Invocation requires an existing private network path; IAM/Tailscale membership
alone does not establish one. Tokens stay in memory, redirects are refused and
no listener opens on the caller. `invoke --path /endpoint --data request.json`
POSTs a body; requests/responses are bounded to16MiB. Jobs enforce one task,
parallelism one and zero task retries. An active execution blocks another local
request, but concurrent callers can race: these are not global spending caps.
The owner must enforce Google quotas and spending controls before activation.

For simple images use `--cpu`/`--memory`. `--file workload.json` instead of
`--image` accepts a native container spec: `containers`, optional `volumes`,
`nodeSelector`, `timeoutSeconds`, `containerConcurrency`. This supports GPU,
sidecars, commands and storage without project logic in tailworker. Production
images should be digest-pinned; specs contain public settings/references, never
inline secrets. tailworker supplies identity and enclosing resource policy.
Existing IAM bindings, conditions and etags are preserved; only missing approved
execution grants are added. Unexpected/public grants require owner review.

Both kinds force all egress through the configured VPC. Its firewall must block
connections back to submitters/LAN; inherited Google administrator permissions
and private routing remain owner responsibilities. Deployment/deletion are
explicit owner actions. Service HTTP requests have no universal cancellation;
use Jobs for cancellable batch work. Offline tests do not verify live Google
IAM, quotas, GPU support or network isolation and create no cloud resources.

## Dedicated cloud workers: owner only

The owner first merges `policy.hujson` into the existing tailnet and passes its
tests. Create an OAuth client scoped to auth-key writes for the worker tags,
then run `tailworker setup` on the control host and enter its credentials at
the masked prompts. In the Google project, prepare the `tailworker` network,
subnet and outbound Cloud NAT, with no inbound firewall rules. Install and
authenticate `gcloud` on the control host. No GitHub CLI is required. Fill `[gcp]` in config.toml:
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
register only their public halves read-only through GitHub's HTTPS REST API.
Only this optional automation needs an owner API token. Save one plain token
in the control host's `~/.config/tailworker/github-token` (or the corresponding
XDG config directory), owned by the caller with mode 0600. Create/edit the file
privately outside Git; never put its value in argv, environment or a job.
A fine-grained personal token needs repository Administration (write), which
includes the read permission to list keys, for only the repositories supplied
to `--deploy-key`; Metadata (read) is implicit. See
[GitHub's deploy-key permissions](https://docs.github.com/en/rest/deploy-keys/deploy-keys).
The token stays on the control host and never reaches a worker. Ordinary jobs
and public-dependency workers need no GitHub administration credential. Private
fetch credentials can also be provisioned separately, with the public key
registered manually in the provider's browser UI, without this API automation.
Read-only source tokens belong to the consuming project's fetch secrets and
are separate from this optional Administration token. A source token cannot
perform these deploy-key administration operations.
`--deploy-dir` selects their private directory and `--deploy-base` selects the
paths consumers see inside their secret mount; for example, ci-buddy uses
`.ci-buddy/projects/<project>/secrets` and `/run/secrets`. GitHub host keys come
from its HTTPS metadata API. Private halves stay on the worker.

`down` removes only deploy keys with the exact `tw-NAME` title in the explicit
`tw-deploy-repos` repository scope recorded by `up`, and deletes the labelled VM. Its root
shutdown unit logs out; the job account has no Tailscale administration.
The control host must observe the named node absent before cleanup succeeds
and its alias is removed; an offline node still counts as present. A recorded
empty repository scope needs no GitHub credential. Key lists follow GitHub's
[native pagination links](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api).
Missing scope, an expired VM whose metadata is gone, or inadequate API authority
leaves key cleanup explicitly unverified; no account-wide discovery or deletion
is attempted. The owned VM is still deleted, and the owner checks any remaining
keys manually. Failed cleanup names what remains; no action is retried. VM deletion at the hard lifetime is
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
The command, Cloud Run adapter, bootstrap and check stay below 1,200 lines;
tests stay below 500. These are hard caps. Their unit checks do not replace the owned-host
container, GPU, transfer and cancellation checks or the owner's cloud check.
