# compare_sosreports.py

Compare two RHEL 9 [sosreport](https://github.com/sosreport/sos) archives and
produce a Markdown diff report covering the artifacts that matter most for
host-to-host comparison — OS/kernel version, installed RPMs, kernel modules,
sysctl values, SELinux config, fstab/mounts, block devices, disk usage,
firewalld zones, and systemd unit enablement.

It deliberately **skips** artifacts that are certain to differ between any
two hosts and add no diagnostic signal — hostname, IP addresses, routing
table, and collection timestamp.

Python 3 standard library only — no dependencies to install.

## Usage

```
python3 compare_sosreports.py <sosreport_1> <sosreport_2> [-o report.md]
```

**Arguments:**

| Argument | Description |
|---|---|
| `sosreport_1`, `sosreport_2` | Each may be a sosreport archive (`.tar.xz`, `.tar.gz`, `.tar.bz2`) or a path to an already-extracted sosreport directory. |
| `-o`, `--output` | Path to write the Markdown report to. Default: `sosreport-comparison.md` |
| `-W`, `--width` | Total width of the side-by-side diff output, like `diff -y -W`. Default: `130` |

**Example:**

```
python3 compare_sosreports.py sosreport-node1-2026-07-02.tar.xz sosreport-node2-2026-07-02.tar.xz -o node1-vs-node2.md
```

Archives are extracted to a temporary directory that is cleaned up
automatically when the script exits.

## What it compares

| Section | Source artifact | Comparison method |
|---|---|---|
| OS Release | `etc/os-release` | key/value diff |
| RHEL Release | `etc/redhat-release` | side-by-side (`diff -y` style) |
| Kernel Version | `sos_commands/kernel/uname_-a` | side-by-side (`diff -y` style) |
| CPU | `proc/cpuinfo` | model name + logical CPU count |
| Memory | `proc/meminfo` | MemTotal/MemFree/MemAvailable/SwapTotal/SwapFree |
| Installed RPMs | `installed-rpms` | package set diff (added/removed) + version diff for packages present in both |
| Loaded Kernel Modules | `sos_commands/kernel/lsmod` | module name set diff |
| sysctl -a | `sos_commands/kernel/sysctl_-a` | key/value diff, only differing keys shown |
| SELinux Config | `etc/selinux/config` | key/value diff |
| /etc/fstab | `etc/fstab` | side-by-side (`diff -y` style) |
| Active Mounts | `sos_commands/filesys/findmnt` (or `mount_-l`) | side-by-side (`diff -y` style) |
| Block Devices | `sos_commands/block/lsblk` | side-by-side (`diff -y` style) |
| Disk Usage | `sos_commands/filesys/df_-al_-x_autofs` (or `df`) | side-by-side (`diff -y` style) |
| Firewalld Zones | `sos_commands/firewalld/firewall-cmd_--list-all-zones` | side-by-side (`diff -y` style) |
| Systemd Unit Enablement | `sos_commands/systemd/systemctl_list-unit-files` | per-unit enabled/disabled state diff, only differing units shown |
| Running Processes | `sos_commands/process/ps_auxfwww` (or `ps_auxwwwm`/`ps_alxwww`/`ps_-elfL`) | command-line set diff, PID/CPU/MEM/START/TIME columns ignored |
| Listening Sockets (TCP/UDP) | `ss`/`netstat` output, falling back to `/proc/net/tcp[6]` | LISTEN-state entry set diff |

File lookups use glob patterns with fallbacks (e.g. `ip_-d_address` →
`ip_address`) since exact `sos_commands` filenames can shift slightly
across `sos` package versions. If an artifact is missing from one or both
reports, that's reported explicitly rather than silently skipped.

### Side-by-side diff format

Free-text artifacts (fstab, mounts, lsblk, df, firewalld zones, release/
kernel strings) are rendered like `diff -y`: two columns, left = first
report, right = second report, inside a fenced code block. Only differing
lines are shown — matching lines are omitted, same idea as running
`diff -y` with zero context. Markers, same meaning as `diff -y`:

| Marker | Meaning |
|---|---|
| `\|` | line present on both sides, but changed |
| `<` | line only in the first report (left column) |
| `>` | line only in the second report (right column) |

Long lines are truncated with `…` to fit the column width; use `-W/--width`
to widen or narrow the output if lines are getting cut off.

## Output

A single Markdown file containing:

1. A **Summary** listing which sections differ and which are identical.
2. One subsection per artifact, showing either `_identical_` or the actual
   diff (unified diff for free-text files, a Markdown table for key/value
   and RPM comparisons).

The script also prints a one-line summary to stdout, e.g.:

```
Compared 15 artifact sections; 2 differ. Report written to: report.md
```

## Known limitations

- Large free-text artifacts (e.g. `dmesg`, full `journalctl` output) are
  not compared — they're too noisy for a line diff to be useful and don't
  currently have a dedicated parser.
- Container/podman artifacts (`sos_commands/podman/*`) are not covered.
  If your sosreports include the podman plugin, this is a natural
  extension.
- `sysctl -a` and `installed-rpms` diffs assume the standard `sos`-package
  output format; heavily customized or very old sosreport versions may not
  parse cleanly (the script falls back to reporting the artifact as
  missing/differing rather than crashing).
- **Running Processes**: the diff compares the *set* of command lines —
  PID, %CPU, %MEM, VSZ/RSS, START, and TIME columns are stripped out since
  they're host- and moment-specific noise. On top of that, the command
  text itself is normalized to remove incidental per-instance identifiers
  that would otherwise make the same process look "different" every time:
  - Long hex tokens (8-64 chars, must contain a letter a-f) → `<HASH>`.
    This is aimed at container/image IDs — e.g. `conmon` process lines
    from podman repeat the same 64-char container ID up to 8 times per
    line, and that ID regenerates on every container restart, so without
    this the same container would never match between two captures, or
    between two hosts running the same service.
  - UUIDs (`8-4-4-4-12` hex) → `<UUID>`.
  - systemd session scope numbers (`session-123.scope`) → `session-<N>.scope`.
  - Kernel per-CPU thread names (`[kworker/3:1-xfs]`, `[ksoftirqd/2]`,
    `[migration/0]`, etc.) have their numeric core index collapsed to `N`,
    since these vary purely with CPU count/topology, not with anything
    meaningful about what's running.

  This is deliberately conservative: plain numeric tokens (ports, PIDs
  written as bare digits, buffer sizes, timeouts) are left untouched, so
  genuine configuration differences still surface. It won't catch every
  possible source of incidental noise — e.g. random `/tmp/tmpXXXXXX`
  suffixes or embedded epoch timestamps aren't normalized — so some
  false positives are still possible on more exotic command lines.
- **Listening Sockets**: the script tries `ss`/`netstat` output first
  (several common filename variants), and falls back to decoding
  `/proc/net/tcp` and `/proc/net/tcp6` directly if neither was collected.
  The `/proc/net/tcp[6]` fallback only extracts the port number (not the
  full decoded IP), since the port is what matters most for this kind of
  diff. UDP sockets aren't included in the fallback path, since UDP has no
  true "LISTEN" state in `/proc/net/udp`.
