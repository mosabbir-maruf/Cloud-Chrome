# Cloud-Chrome — Performance

How the runtime behaves in a real Codespace, how it was measured, and
what was changed because of the measurement.

- [The method](#the-method)
- [The environment](#the-environment)
- [The baseline](#the-baseline)
- [The optimisation](#the-optimisation)
- [The results](#the-results)
- [The remaining bottlenecks](#the-remaining-bottlenecks)
- [The limitations](#the-limitations)
- [Reproducing it](#reproducing-it)

---

## The method

The probe is `tests/probe/performance-probe.sh`. It runs **inside** a real
GitHub Codespaces dev container, it uses the runtime's own commands, and
it publishes its measurements as an issue in this repository through the
same mechanism as the runtime probe.

**The isolation.** The probe never touches the state of the repository.
It points `CLOUDCHROME_PERSIST_DIR` and `HOME` at a temporary directory
that is removed on exit, and it fills the profile with three synthetic
files. The real `.cloudchrome` directory is never read, copied, backed up
or restored, and no cookie, no browsing history, no password, no age
identity, no passphrase and no VNC credential is ever in the evidence.

**The samples.** The startup and the shutdown are sampled ten times, the
backup and the restore five times, and the warm start ten times. Every
sample is a complete operation: the startup is a stop, a start and a wait
until the desktop reports itself healthy; the shutdown is a stop and a
wait until every supervised process has exited; the backup and the
restore are whole commands.

**The summary.** The median is the middle value of the sorted samples.
The p95 is the nearest-rank percentile: the value at the `ceil(0.95 · n)`
position of the sorted list, counting the first value as position one.
Both are reported, because a single number does not describe a
distribution. The raw samples are in the evidence, so the summary can be
recomputed.

**The noise.** The measurement machine is shared. A run of the probe on a
busy host shows a slower shutdown and a slower warm start than the same
code on a quiet one, even though nothing about those code paths
changed. Two things guard against that:

1. The **idle CPU is measured from the processes of the desktop**, not
   from `/proc/stat`. Inside a container `/proc/stat` holds the counters
   of the **host**, so they move with every other tenant of the machine.
   The host counters are reported as well, and they are labelled as
   context.
2. The probe can measure the **same startup twice in the same
   container**: once with the code of the checkout and once with the code
   of another commit, swapping only the runtime. The two numbers then
   come from the same machine, under the same conditions and within the
   same minutes.

**What is not measured.** The time GitHub takes to provision, create or
rebuild a Codespace is not measured here, and it cannot be: the probe
runs inside the container and has no view of the platform. Every number
below is the runtime of the container.

---

## The environment

| | |
|---|---|
| The platform | GitHub Codespaces |
| The machine type | `basicLinux32gb` — 2 CPUs, 8 GB RAM, 32 GB storage |
| The measured CPU count | 2 |
| The measured memory | 8 328 060 928 bytes (7.76 GiB) |
| The operating system | Ubuntu 24.04.3 LTS |
| The kernel | `6.8.0-1070-azure` |
| The runtime | Cloud-Chrome 2.0.0 |
| Chrome | 157.0.8103.50 |
| The user | `vscode`, uid 1000 |
| The image | 662 221 650 bytes uncompressed (631 MiB) |
| The build duration | 126 s on the CI runner, with the package cache warm |

The machine type is not reported inside the container, so it was taken
from the request that created the Codespace. The measured CPU count and
the measured memory match it.

**The default is not changed.** The request stays at 2 CPUs and 8 GB.
The measurements do not show that the workload needs more: the desktop
uses about 360 MiB and 4 % of one CPU while it is idle, and the machine
has 8 GB.

---

## The baseline

Measured on 2026-11-12 with the code of commit `c6bc7f2`, before the
optimisation. The evidence is
[issue #30](https://github.com/mosabbir-maruf/Cloud-Chrome/issues/30).

| Measurement | min | median | p95 | max |
|---|---|---|---|---|
| The startup of the desktop | 4 113 ms | **4 222 ms** | 5 291 ms | 5 291 ms |
| The shutdown | 358 ms | **509 ms** | 861 ms | 861 ms |
| The warm start | 64 ms | **65 ms** | 75 ms | 75 ms |
| The backup | 6 689 ms | **6 793 ms** | 6 889 ms | 6 889 ms |
| The restore | 5 415 ms | **5 441 ms** | 5 512 ms | 5 512 ms |
| The noVNC response | — | **3.99 ms** | — | 7.56 ms |

| Resource | Value |
|---|---|
| The memory of the cgroup | 960 970 752 bytes (917 MiB) |
| The proportional set size of the desktop | 380 135 424 bytes (363 MiB) |
| The resident set size of the desktop | 683 761 664 bytes (652 MiB) |
| The busy share of the host over 30 s | 6.41 % |
| The archive of a backup | 42 801 107 bytes (41 MiB), 459 members |
| The noVNC port | answers `HTTP 200`, 0 failed requests |
| The RFB port | `RFB 003.008`, the security type `2` (VncAuth) |

### Where the startup went

The supervisor writes a timestamped line for every step of the startup,
and the probe publishes the tail of that log. The timeline of the last
startup of the baseline run:

```
10:46:31  started xvnc
10:46:32  started dbus
10:46:32  started keyring
10:46:32  the keyring daemon serves the secret service
10:46:32  the secret service stored and returned a probe secret
10:46:32  started desktop
10:46:33  started novnc
10:46:33  started chrome
10:46:35  the desktop stack is running      <-- two seconds later
```

**Two seconds of the four were a fixed sleep.** The supervisor slept for
two seconds after it started Chrome, to give the browser a chance to
fail, and slept for two seconds of the startup of every sample.

The rest was the time the runtime spent waiting for a poll: the start
command polled the state file of the supervisor every 500 ms, the
supervisor polled the readiness of each of its components every 250 ms,
and the keyring polled for the Secret Service every 250 ms.

---

## The optimisation

Four changes, none of which touches what the desktop does. They are in
`lib/cloudchrome/lifecycle.py`, `lib/cloudchrome/supervisor.py` and
`lib/cloudchrome/keyring.py`.

1. **The sleep after Chrome became a condition.** The browser is given a
   second to fail, and a Chrome that exits is reported at once instead of
   after the whole wait. The failure was observed to take a tenth of that,
   so the margin is wide. A Chrome that fails later is reported by the
   monitor loop, as before.
2. **The start command polls the state file every 50 ms** instead of
   every 500 ms. The supervisor writes the state once, when the stack is
   up, so the time spent waiting for the poll was time the user spent
   waiting for the desktop.
3. **The supervisor polls the readiness of its components every 50 ms**
   instead of every 250 ms. The predicates are a socket, a port or a
   process.
4. **The keyring polls for the Secret Service every 250 ms**, as before.
   The interval was tried at 100 ms and the gain was not worth the extra
   bus traffic, so it was left where it was.

Nothing was removed, no flag was added to Chrome, the sandbox was not
touched, and the shared memory was not changed.

### The defect the optimisation exposed

The CI caught a real bug that the optimisation made visible: **the
supervisor did not honour a stop request while the keyring was
starting.** The signal handler set the stopping flag, but the waits of
the keyring step did not look at it, so a `SIGTERM` that arrived while
the keyring was coming up was ignored until the keyring had finished its
own waits — long enough for the CI test to time out.

The waits of the keyring step now take a callback that raises when a stop
was requested, and the bus and the secret tool are given the time that
is left, so the waits are bounded by their own timeout. The defect was
there before the optimisation; the optimisation only made it happen more
often. It is fixed in `lib/cloudchrome/supervisor.py` and
`lib/cloudchrome/keyring.py`.

---

## The results

### The same-container comparison

The strongest of the measurements: the same startup, ten times, with the
optimised runtime and with the runtime of `c6bc7f2`, in the same
container and within the same minutes. It was run three times, in three
independent Codespaces, and the three agree.

The evidence is
[issue #36](https://github.com/mosabbir-maruf/Cloud-Chrome/issues/36),
[issue #38](https://github.com/mosabbir-maruf/Cloud-Chrome/issues/38) and
[issue #40](https://github.com/mosabbir-maruf/Cloud-Chrome/issues/40),
the last of which is the run of the release candidate. The runtime probe
of the same run is issue
[#41](https://github.com/mosabbir-maruf/Cloud-Chrome/issues/41). The runtime probe of
the same run is issue
[#39](https://github.com/mosabbir-maruf/Cloud-Chrome/issues/39).

| The code | min | median | p95 | max |
|---|---|---|---|---|
| `c6bc7f2`, before the optimisation | 4 117 ms | **4 892 ms** | 6 123 ms | 6 123 ms |
| The optimised runtime | 2 153 ms | **2 454 ms** | 3 306 ms | 3 306 ms |
| **The difference, the first run** | | **−49.8 %** | −46.0 % | −46.0 % |
| `c6bc7f2`, before the optimisation | 4 183 ms | **4 802 ms** | 6 328 ms | 6 328 ms |
| The optimised runtime | 2 217 ms | **2 287 ms** | 2 836 ms | 2 836 ms |
| **The difference, the second run** | | **−52.4 %** | −55.2 % | −55.2 % |
| `c6bc7f2`, before the optimisation | 4 163 ms | **4 792 ms** | 5 205 ms | 5 205 ms |
| The optimised runtime | 2 183 ms | **2 441 ms** | 3 253 ms | 3 253 ms |
| **The difference, the third run** | | **−49.1 %** | −37.5 % | −37.5 % |

### The whole picture

The "before" column is the baseline run, the "after" column is the final
run, and both are in the same machine type.

| Measurement | Before | After | The change |
|---|---|---|---|
| The startup, median | 4 222 ms | **2 441 ms** | **−42 %** |
| The startup, same container | 4 892 / 4 802 / 4 792 ms | **2 454 / 2 287 / 2 441 ms** | **−50 % / −52 % / −49 %** |
| The shutdown, median | 509 ms | 638 ms | within the noise |
| The warm start, median | 65 ms | 67 ms | within the noise |
| The backup, median | 6 793 ms | **5 345 ms** | **−21 %** |
| The restore, median | 5 441 ms | **4 308 ms** | **−21 %** |
| The proportional set size | 363 MiB | 359 MiB | unchanged |
| The idle CPU of the desktop | not measured in a container way | 3.87 % of one CPU | measured |
| The archive of a backup | 41 MiB, 459 members | 41 MiB, 461 members | unchanged |

The backup and the restore are faster for the same reason: the backup
stops the desktop for the snapshot and starts it again, and the restore
starts the restored desktop. Both pay for a startup.

The shutdown and the warm start are unchanged in the code, and their
numbers moved by less than the noise of the host.

### The idle CPU

Measured over a 30-second window with the desktop at rest, from the
user and the system time of the processes of the desktop:

| | |
|---|---|
| The CPU time of the desktop | 1.160 s in 30 s |
| The share of one CPU | 3.87 % |
| The share of the two CPUs of the machine | 1.93 % |
| The busy share of the host in the same window | 4.46 % |

The desktop is almost all of the load of the machine in that window. The
host counters are reported because they are the context, and they are
explicitly **not** a measure of the desktop: a run of the probe on a
busy host reported 29.57 % for the host while the desktop itself used
3.87 % of one CPU. Two independent runs measured 3.87 % and 3.93 % for
the desktop, which is the repeatability of the metric.

---

## The remaining bottlenecks

**The profile is large, and most of it is not the user's data.** A fresh
Chrome profile in a Codespace is 41 MiB in about 460 files, and the great
majority of it is Chrome's own caches and downloaded components: the
optimisation guide model store, the component and extension download
caches, the Widevine component, the GPU and the Dawn caches. Those are
re-downloaded by Chrome when it needs them.

They are **in** the backup today, so the archive is 41 MiB and the
archiving is the larger part of the backup that is not the restart. They
could be left out, which would make the archive a fraction of its size
and the backup and the restore faster.

They were **not** left out. A backup exists to restore a state, and
which files it holds is a decision about recovery, not about a
benchmark. The change is worth doing, and it is recorded here as a
finding rather than taken here.

**The archive is read twice.** The manifest carries the hash of every
member, and the manifest is written into the archive before the members
are streamed, so the profile is hashed and then read again for the
archive. The restore reads the archive twice for the same reason. The
cost is small compared to the compression, but it is there.

**The profile is compressed at the default level.** The tar stream is
gzipped at the default level, which is a good trade between time and
size for a 41 MiB profile. A lower level would make the backup faster
and the archive bigger.

**The startup still waits for the components in order.** The keyring has
to be ready before Chrome, and Chrome has to be ready before the stack
is declared up. The desktop and the noVNC bridge do not depend on the
keyring, so they could be started alongside it. That would be a change
to the structure of the startup, and the gain would be a few hundred
milliseconds at the cost of a much harder-to-reason-about startup. It
was not done.

---

## The limitations

- **The numbers are from one machine type.** They were measured on
  `basicLinux32gb`. A different machine, or a Codespace with a warm page
  cache, gives different numbers.
- **The host is shared.** The shutdown and the warm start move with the
  load of the host. The same-container comparison is the one to trust.
- **The sample count is small.** Ten samples of a startup and five of a
  backup are enough to see a 50 % difference and not enough to characterise
  a distribution to the last millisecond. The p95 of ten samples is the
  largest sample, which is a weakness of the method and the reason the
  raw samples are published.
- **The probe measures the container, not the platform.** The time
  GitHub takes to create or rebuild a Codespace is not in any of these
  numbers.
- **The idle CPU is the CPU of the desktop at rest.** A desktop with a
  busy page in it uses more, and that is not measured.
- **The image size and the build duration come from the CI runner**, not
  from the Codespace, because a container cannot measure the image it
  runs in.

---

## Reproducing it

The probe needs a Codespace, because that is where the image and the
runtime live.

```bash
# In a Codespace created from a branch that runs the probe:
CLOUDCHROME_PROBE_PUBLISH=1 \
CLOUDCHROME_PERFORMANCE_OUTPUT=/workspaces/Cloud-Chrome/performance-evidence.txt \
bash tests/probe/performance-probe.sh

# With a comparison against another commit, in the same container:
CLOUDCHROME_PERF_COMPARE_COMMIT=<sha> \
CLOUDCHROME_PROBE_PUBLISH=1 \
CLOUDCHROME_PERFORMANCE_OUTPUT=/workspaces/Cloud-Chrome/performance-evidence.txt \
bash tests/probe/performance-probe.sh
```

The number of samples and the length of the idle window can be changed
with `CLOUDCHROME_PERF_STARTUP_SAMPLES`,
`CLOUDCHROME_PERF_BACKUP_SAMPLES` and `CLOUDCHROME_PERF_IDLE_SECONDS`.

CI checks that the probe is well formed — that its syntax is valid, that
it stays away from the real state directory and that it cleans up after
itself — with `tests/probe/check-probes.sh`. **CI does not check a wall
clock threshold.** The GitHub runner is too noisy for a narrow one, and a
threshold that flakes teaches everyone to ignore it.
