#!/usr/bin/env python3
"""
compare_sosreports.py

Compare two RHEL 9 sosreport archives (or already-extracted sosreport
directories) and produce a Markdown diff report covering the artifacts
that matter most for host-to-host comparison: OS/kernel version, installed
RPMs, kernel modules, sysctl values, SELinux config, fstab/mounts, block
devices, network interfaces, enabled services, firewalld config, CPU/memory.

Usage:
    python3 compare_sosreports.py <sosreport_1> <sosreport_2> [-o report.md]

<sosreport_1>/<sosreport_2> may each be:
    - a .tar.xz / .tar.gz / .tar.bz2 sosreport archive, or
    - a path to an already-extracted sosreport directory

Only standard library is used, so no extra installs are required.
"""

import argparse
import difflib
import glob
import itertools
import os
import re
import sys
import tarfile
import tempfile
from collections import OrderedDict


# ---------------------------------------------------------------------------
# Extraction / location helpers
# ---------------------------------------------------------------------------

def extract_safe(tf, dest):
    """Extract only regular files, directories, and (sym/hard) links from a
    tar archive, skipping special files (device nodes, FIFOs, sockets).

    sosreport tarballs sometimes include placeholder entries for things
    like /dev nodes or /run/systemd/sessions/*.ref that are captured as
    tar "special" member types. Python 3.12+'s default extraction filter
    (PEP 706) raises tarfile.SpecialFileError on these, even though we
    never need them for a config/artifact comparison — so filter them out
    up front instead.
    """
    members = [m for m in tf.getmembers()
               if m.isreg() or m.isdir() or m.issym() or m.islnk()]
    try:
        tf.extractall(dest, members=members, filter="data")
    except TypeError:
        # Python < 3.12 doesn't support the `filter` kwarg.
        tf.extractall(dest, members=members)


def prepare_root(path, tmp_base):
    """Return a directory that is the root of an extracted sosreport tree.

    Accepts either a directory (used as-is, drilling into a single nested
    'sosreport-*' folder if present) or a tar archive (extracted to a temp
    dir first).
    """
    if os.path.isdir(path):
        root = path
    elif os.path.isfile(path) and tarfile.is_tarfile(path):
        dest = tempfile.mkdtemp(dir=tmp_base)
        with tarfile.open(path) as tf:
            extract_safe(tf, dest)
        root = dest
    else:
        sys.exit(f"error: '{path}' is not a directory or a readable tar archive")

    # sosreport archives contain a single top-level sosreport-<host>-... dir
    entries = [e for e in os.listdir(root) if not e.startswith(".")]
    if len(entries) == 1 and os.path.isdir(os.path.join(root, entries[0])):
        candidate = os.path.join(root, entries[0])
        if entries[0].startswith("sosreport-") or os.path.isdir(
            os.path.join(candidate, "sos_commands")
        ):
            root = candidate

    if not os.path.isdir(os.path.join(root, "sos_commands")) and not os.path.isdir(
        os.path.join(root, "etc")
    ):
        print(f"warning: '{root}' doesn't look like a typical sosreport root "
              f"(no sos_commands/ or etc/ found) — continuing anyway", file=sys.stderr)
    return root


def find_first(root, *rel_globs):
    """Return the first existing file under root matching any of the given
    relative glob patterns."""
    for pattern in rel_globs:
        matches = sorted(glob.glob(os.path.join(root, pattern)))
        if matches:
            return matches[0]
    return None


def read_lines(path):
    if not path or not os.path.isfile(path):
        return None
    with open(path, "r", errors="replace") as f:
        return f.read().splitlines()


def hostname_of(root):
    for rel in ("sos_commands/host/hostname", "etc/hostname"):
        lines = read_lines(os.path.join(root, rel))
        if lines and lines[0].strip():
            return lines[0].strip()
    return os.path.basename(root)


# ---------------------------------------------------------------------------
# Report building blocks
# ---------------------------------------------------------------------------

class Report:
    def __init__(self, name_a, name_b):
        self.name_a = name_a
        self.name_b = name_b
        self.sections = []  # list of (title, body_markdown, changed: bool)

    def add(self, title, body, changed):
        self.sections.append((title, body, changed))

    def render(self):
        out = []
        out.append(f"# sosreport comparison: `{self.name_a}` vs `{self.name_b}`\n")
        changed = [t for t, _, c in self.sections if c]
        unchanged = [t for t, _, c in self.sections if not c]
        out.append("## Summary\n")
        if changed:
            out.append("**Sections with differences:**")
            for t in changed:
                out.append(f"- {t}")
        else:
            out.append("No differences found in any compared section.")
        if unchanged:
            out.append("\n**Sections identical / no differences:** " + ", ".join(unchanged))
        out.append("\n---\n")
        for title, body, changed_flag in self.sections:
            marker = "DIFFERS" if changed_flag else "identical"
            out.append(f"## {title} ({marker})\n")
            out.append(body if body.strip() else "_no data available in one or both reports_")
            out.append("")
        return "\n".join(out)


DIFF_WIDTH = 130  # total width of the side-by-side block, like `diff -y -W`


def _fit(text, col_width):
    """Pad/truncate a line to exactly col_width chars, like diff -y does."""
    text = text.rstrip("\n")
    if len(text) > col_width:
        return text[: col_width - 1] + "\u2026"  # ellipsis
    return text.ljust(col_width)


def side_by_side_diff(label_a, label_b, lines_a, lines_b, width=DIFF_WIDTH):
    """Render a `diff -y` style side-by-side comparison, wrapped in a
    fenced code block. Only differing regions are shown (no equal/context
    lines), same philosophy as the old context=0 unified diff.

    Markers, matching `diff -y`:
        <   line only on the left  (label_a)
        >   line only on the right (label_b)
        |   line present on both sides but changed
    Returns (markdown_body, changed_bool).
    """
    if lines_a is None and lines_b is None:
        return "_artifact not present in either report_", False
    if lines_a is None or lines_b is None:
        missing = label_a if lines_a is None else label_b
        return f"_artifact missing from **{missing}**_", True

    col = max(10, (width - 3) // 2)
    sm = difflib.SequenceMatcher(a=lines_a, b=lines_b, autojunk=False)

    rows = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        a_seg, b_seg = lines_a[i1:i2], lines_b[j1:j2]
        if tag == "replace":
            for a, b in itertools.zip_longest(a_seg, b_seg):
                if a is not None and b is not None:
                    rows.append(f"{_fit(a, col)} | {b}")
                elif a is not None:
                    rows.append(f"{_fit(a, col)} <")
                else:
                    rows.append(f"{_fit('', col)} > {b}")
        elif tag == "delete":
            for a in a_seg:
                rows.append(f"{_fit(a, col)} <")
        elif tag == "insert":
            for b in b_seg:
                rows.append(f"{_fit('', col)} > {b}")

    if not rows:
        return "_identical_", False

    header = f"{_fit(label_a, col)}   {label_b}"
    sep = f"{'-' * col}   {'-' * min(col, 40)}"
    body = "```\n" + header + "\n" + sep + "\n" + "\n".join(rows) + "\n```"
    return body, True


def keyvalue_lines(lines, sep="="):
    """Parse simple KEY=value or KEY: value lines into an OrderedDict."""
    result = OrderedDict()
    if not lines:
        return result
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if sep in line:
            k, _, v = line.partition(sep)
            result[k.strip()] = v.strip().strip('"')
    return result


def diff_keyvalue(label_a, label_b, lines_a, lines_b, sep="="):
    if lines_a is None and lines_b is None:
        return "_artifact not present in either report_", False
    if lines_a is None or lines_b is None:
        missing = label_a if lines_a is None else label_b
        return f"_artifact missing from **{missing}**_", True
    kv_a = keyvalue_lines(lines_a, sep=sep)
    kv_b = keyvalue_lines(lines_b, sep=sep)
    keys = sorted(set(kv_a) | set(kv_b))
    rows = []
    changed = False
    for k in keys:
        va, vb = kv_a.get(k), kv_b.get(k)
        if va != vb:
            changed = True
            rows.append(f"| `{k}` | {va if va is not None else '_(absent)_'} | "
                        f"{vb if vb is not None else '_(absent)_'} |")
    if not rows:
        return "_identical_", False
    body = f"| Key | {label_a} | {label_b} |\n|---|---|---|\n" + "\n".join(rows)
    return body, changed


# ---------------------------------------------------------------------------
# Specific artifact comparators
# ---------------------------------------------------------------------------

RPM_RE = re.compile(r"^(?P<name>.+)-(?P<ver>[^-]+)-(?P<rel>[^-]+)\.(?P<arch>[a-z0-9_]+)$")


def parse_rpms(lines):
    """installed-rpms format: 'name-version-release.arch  installtime  ...'
    Returns dict: pkgname -> full nvra string (first whitespace-delimited field)."""
    pkgs = {}
    if not lines:
        return pkgs
    for line in lines:
        if not line.strip() or line.startswith("gpg-pubkey"):
            continue
        nvra = line.split()[0]
        m = RPM_RE.match(nvra)
        if m:
            pkgs[m.group("name")] = nvra
        else:
            pkgs[nvra] = nvra
    return pkgs


def diff_rpms(label_a, label_b, lines_a, lines_b):
    if lines_a is None and lines_b is None:
        return "_installed-rpms not present in either report_", False
    if lines_a is None or lines_b is None:
        missing = label_a if lines_a is None else label_b
        return f"_installed-rpms missing from **{missing}**_", True

    pa, pb = parse_rpms(lines_a), parse_rpms(lines_b)
    only_a = sorted(set(pa) - set(pb))
    only_b = sorted(set(pb) - set(pa))
    common_diff = sorted(k for k in (set(pa) & set(pb)) if pa[k] != pb[k])

    changed = bool(only_a or only_b or common_diff)
    parts = [f"Package count: {label_a}={len(pa)}, {label_b}={len(pb)}\n"]

    if common_diff:
        parts.append(f"**Version differences ({len(common_diff)}):**\n")
        parts.append("| Package | " + label_a + " | " + label_b + " |")
        parts.append("|---|---|---|")
        for k in common_diff:
            parts.append(f"| {k} | {pa[k]} | {pb[k]} |")
        parts.append("")

    if only_a:
        parts.append(f"**Only in {label_a} ({len(only_a)}):**")
        parts.append("```\n" + "\n".join(pa[k] for k in only_a) + "\n```")

    if only_b:
        parts.append(f"**Only in {label_b} ({len(only_b)}):**")
        parts.append("```\n" + "\n".join(pb[k] for k in only_b) + "\n```")

    if not changed:
        parts.append("_identical package set and versions_")

    return "\n".join(parts), changed


def parse_meminfo(lines):
    kv = {}
    for line in lines or []:
        if ":" in line:
            k, v = line.split(":", 1)
            kv[k.strip()] = v.strip()
    return kv


def diff_meminfo(label_a, label_b, lines_a, lines_b):
    if lines_a is None and lines_b is None:
        return "_proc/meminfo not present in either report_", False
    if lines_a is None or lines_b is None:
        missing = label_a if lines_a is None else label_b
        return f"_proc/meminfo missing from **{missing}**_", True
    ma, mb = parse_meminfo(lines_a), parse_meminfo(lines_b)
    keys = ["MemTotal", "MemFree", "MemAvailable", "SwapTotal", "SwapFree"]
    rows = []
    changed = False
    for k in keys:
        va, vb = ma.get(k, "n/a"), mb.get(k, "n/a")
        if va != vb:
            changed = True
        rows.append(f"| {k} | {va} | {vb} |")
    body = f"| Field | {label_a} | {label_b} |\n|---|---|---|\n" + "\n".join(rows)
    return body, changed


def parse_cpuinfo(lines):
    model, count = None, 0
    for line in lines or []:
        if line.startswith("model name") and model is None:
            model = line.split(":", 1)[1].strip()
        if line.startswith("processor"):
            count += 1
    return model, count


def diff_cpuinfo(label_a, label_b, lines_a, lines_b):
    if lines_a is None and lines_b is None:
        return "_proc/cpuinfo not present in either report_", False
    if lines_a is None or lines_b is None:
        missing = label_a if lines_a is None else label_b
        return f"_proc/cpuinfo missing from **{missing}**_", True
    model_a, cnt_a = parse_cpuinfo(lines_a)
    model_b, cnt_b = parse_cpuinfo(lines_b)
    changed = (model_a != model_b) or (cnt_a != cnt_b)
    body = (f"| Field | {label_a} | {label_b} |\n|---|---|---|\n"
            f"| CPU model | {model_a} | {model_b} |\n"
            f"| Logical CPUs | {cnt_a} | {cnt_b} |")
    return body, changed


def diff_sysctl(label_a, label_b, lines_a, lines_b):
    """sysctl -a output is huge; only show keys whose values differ."""
    return diff_keyvalue(label_a, label_b, lines_a, lines_b, sep="=")


def diff_service_list(label_a, label_b, lines_a, lines_b):
    """systemctl list-unit-files style: 'unit  state  preset' -> compare
    enabled/disabled state per unit."""
    if lines_a is None and lines_b is None:
        return "_service unit list not present in either report_", False
    if lines_a is None or lines_b is None:
        missing = label_a if lines_a is None else label_b
        return f"_service unit list missing from **{missing}**_", True

    def parse(lines):
        d = {}
        for line in lines:
            parts = line.split()
            if len(parts) >= 2 and parts[0].endswith((".service", ".socket", ".timer")):
                d[parts[0]] = parts[1]
        return d

    sa, sb = parse(lines_a), parse(lines_b)
    units = sorted(set(sa) | set(sb))
    rows = []
    changed = False
    for u in units:
        va, vb = sa.get(u, "absent"), sb.get(u, "absent")
        if va != vb:
            changed = True
            rows.append(f"| {u} | {va} | {vb} |")
    if not rows:
        return "_identical unit enablement state_", False
    body = (f"Units with differing enablement state ({len(rows)} of {len(units)} total):\n\n"
            f"| Unit | {label_a} | {label_b} |\n|---|---|---|\n" + "\n".join(rows))
    return body, changed


PS_COMMAND_RE = re.compile(r"^\s*(?:USER|UID)\s+PID\b")

# --- Normalization patterns for process command lines -----------------
# Goal: two invocations of "the same" process should normalize to the same
# string even when they embed incidental, per-instance identifiers that
# will never match across hosts (or even across two captures of the same
# host). Observed in the wild: podman/conmon command lines that repeat a
# 64-char container ID up to 8 times per line, systemd session scope
# numbers, and kernel per-CPU thread names like [kworker/3:1-xfs].

_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_HEX_TOKEN_RE = re.compile(r"\b[0-9a-fA-F]{8,64}\b")
_SESSION_SCOPE_RE = re.compile(r"\bsession-\d+\.scope\b")
_KERNEL_THREAD_RE = re.compile(r"^\[.*\]$")


def _collapse_hex_token(match):
    """Replace a bare hex-looking token with <HASH> only if it actually
    contains a letter a-f — pure-digit tokens (PIDs, ports, buffer sizes,
    timeouts) are left untouched so real config differences still show."""
    token = match.group(0)
    return "<HASH>" if re.search(r"[a-fA-F]", token) else token


def normalize_process_cmd(cmd):
    """Strip incidental per-instance identifiers from a process command
    line so that recurring processes (container helpers, kernel threads,
    login sessions) compare equal across captures instead of looking
    unique every time. This is heuristic and intentionally conservative:
    only clearly-identifier-shaped substrings (UUIDs, long hex hashes,
    session scope numbers) are touched; ports, PIDs-as-plain-digits, and
    other genuinely meaningful config values are left alone."""
    if _KERNEL_THREAD_RE.match(cmd):
        # e.g. [kworker/3:1-xfs-conv/vda3], [ksoftirqd/2], [migration/0]
        # -> collapse per-CPU core indices so thread *types* still compare,
        # without false diffs purely from differing CPU counts/topology.
        inner = re.sub(r"\d+", "N", cmd[1:-1])
        return f"[{inner}]"
    cmd = _UUID_RE.sub("<UUID>", cmd)
    cmd = _HEX_TOKEN_RE.sub(_collapse_hex_token, cmd)
    cmd = _SESSION_SCOPE_RE.sub("session-<N>.scope", cmd)
    return cmd


def parse_ps_commands(lines):
    """Parse `ps` output (auxfwww/auxwwwm/alxwww/-elfL style) into a set of
    normalized command strings. PID/CPU/MEM/START/TIME columns are ignored
    since they are host- and moment-specific noise; the extracted command
    text is further normalized (see normalize_process_cmd) before being
    added to the comparison set."""
    commands = set()
    if not lines:
        return commands
    header = None
    for i, line in enumerate(lines):
        if PS_COMMAND_RE.match(line):
            header = line
            break
    start_idx = 1 if header is not None else 0
    # Determine number of leading numeric/text columns to skip before COMMAND.
    # Standard `ps auxww`-family output has 10 leading columns:
    # USER PID %CPU %MEM VSZ RSS TTY STAT START TIME COMMAND...
    # `ps -elfL` has a different layout; fall back to best-effort split.
    for line in lines[start_idx:]:
        if not line.strip():
            continue
        parts = line.split(None, 10)
        if len(parts) < 11:
            # Unexpected layout (e.g. -elfL with extra column) - try 11 fields
            parts = line.split(None, 11)
            if len(parts) < 12:
                continue
            cmd = parts[11]
        else:
            cmd = parts[10]
        cmd = cmd.strip().lstrip("\\_| \t")
        if cmd:
            commands.add(normalize_process_cmd(cmd))
    return commands


def diff_process_set(label_a, label_b, lines_a, lines_b):
    if lines_a is None and lines_b is None:
        return "_process list not present in either report_", False
    if lines_a is None or lines_b is None:
        missing = label_a if lines_a is None else label_b
        return f"_process list missing from **{missing}**_", True

    pa, pb = parse_ps_commands(lines_a), parse_ps_commands(lines_b)
    only_a, only_b = sorted(pa - pb), sorted(pb - pa)
    changed = bool(only_a or only_b)
    parts = [f"Process count (after normalizing container IDs/hashes/session "
             f"numbers): {label_a}={len(pa)}, {label_b}={len(pb)}\n"]
    if only_a:
        parts.append(f"**Only running on {label_a} ({len(only_a)}):**")
        parts.append("```\n" + "\n".join(only_a) + "\n```")
    if only_b:
        parts.append(f"**Only running on {label_b} ({len(only_b)}):**")
        parts.append("```\n" + "\n".join(only_b) + "\n```")
    if not changed:
        parts.append("_identical set of running process command lines_")
    return "\n".join(parts), changed


LISTEN_ADDR_RE = re.compile(r"(\[?[0-9a-fA-F:.*]+\]?:\d+)")


def parse_ss_netstat_listening(lines):
    """Extract LISTEN-state entries from `ss` or `netstat` output as a set
    of 'proto local_addr:port' strings. Column layouts differ between ss
    and netstat, but in both the local address:port is the first
    address-like token on a LISTEN line, so a regex scan is more robust
    than assuming fixed column positions."""
    entries = set()
    for line in lines or []:
        if "LISTEN" not in line.upper():
            continue
        fields = line.split()
        if not fields:
            continue
        proto = fields[0].lower()
        if not proto.startswith(("tcp", "udp")):
            continue
        m = LISTEN_ADDR_RE.search(line)
        if m:
            entries.add(f"{proto} {m.group(1)}")
    return entries


def parse_proc_net_listening(lines, proto_label):
    """Fallback parser for /proc/net/tcp[6] when ss/netstat aren't present
    in the sosreport. Only extracts the port (skips full hex IP decode,
    since the port is what matters most for a listening-socket diff).
    State 0A (hex) = TCP_LISTEN."""
    entries = set()
    for line in (lines or [])[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        local, state = parts[1], parts[3]
        if state.upper() != "0A":
            continue
        if ":" not in local:
            continue
        port_hex = local.split(":")[1]
        try:
            port = int(port_hex, 16)
        except ValueError:
            continue
        entries.add(f"{proto_label} port {port}")
    return entries


def diff_listening_sockets(label_a, label_b, entries_a, entries_b, source_a, source_b):
    if entries_a is None and entries_b is None:
        return "_no ss/netstat/proc-net data available in either report_", False
    if not entries_a and not entries_b:
        return "_no listening sockets found in either report_", False
    only_a, only_b = sorted(entries_a - entries_b), sorted(entries_b - entries_a)
    changed = bool(only_a or only_b)
    parts = [f"Listening sockets: {label_a}={len(entries_a)} (from {source_a}), "
             f"{label_b}={len(entries_b)} (from {source_b})\n"]
    if only_a:
        parts.append(f"**Only listening on {label_a} ({len(only_a)}):**")
        parts.append("```\n" + "\n".join(only_a) + "\n```")
    if only_b:
        parts.append(f"**Only listening on {label_b} ({len(only_b)}):**")
        parts.append("```\n" + "\n".join(only_b) + "\n```")
    if not changed:
        parts.append("_identical set of listening sockets_")
    return "\n".join(parts), changed


# ---------------------------------------------------------------------------
# Main comparison driver
# ---------------------------------------------------------------------------

def get_lines(root, *rel_globs):
    path = find_first(root, *rel_globs)
    return read_lines(path), path


def get_listening_sockets(root):
    """Try ss, then netstat, then /proc/net/tcp[6] as a last resort.
    Returns (entries_set_or_None, source_description)."""
    for pattern, label in [
        ("sos_commands/networking/ss_-tulpn", "ss -tulpn"),
        ("sos_commands/networking/ss_-tulnp", "ss -tulnp"),
        ("sos_commands/networking/ss_-tlnp", "ss -tlnp"),
        ("sos_commands/networking/ss_-tuln", "ss -tuln"),
        ("sos_commands/networking/ss_-natu*", "ss (natu)"),
        ("sos_commands/networking/netstat_-neopa", "netstat -neopa"),
        ("sos_commands/networking/netstat_-tulpn", "netstat -tulpn"),
        ("sos_commands/networking/netstat_-tlnp", "netstat -tlnp"),
        ("sos_commands/networking/netstat_-agn", "netstat -agn"),
    ]:
        lines, _ = get_lines(root, pattern)
        if lines:
            entries = parse_ss_netstat_listening(lines)
            if entries:
                return entries, label

    # Fallback: decode /proc/net/tcp and /proc/net/tcp6 directly.
    entries = set()
    found_any = False
    for rel, label in [("proc/net/tcp", "tcp"), ("proc/net/tcp6", "tcp6")]:
        lines, _ = get_lines(root, rel)
        if lines:
            found_any = True
            entries |= parse_proc_net_listening(lines, label)
    if found_any:
        return entries, "/proc/net/tcp[6]"

    return None, "n/a"


def compare(root_a, root_b, label_a, label_b, width=DIFF_WIDTH):
    report = Report(label_a, label_b)

    # --- OS release ---
    la, _ = get_lines(root_a, "etc/os-release")
    lb, _ = get_lines(root_b, "etc/os-release")
    body, changed = diff_keyvalue(label_a, label_b, la, lb, sep="=")
    report.add("OS Release (/etc/os-release)", body, changed)

    # --- redhat-release ---
    la, _ = get_lines(root_a, "etc/redhat-release")
    lb, _ = get_lines(root_b, "etc/redhat-release")
    body, changed = side_by_side_diff(label_a, label_b, la, lb, width=width)
    report.add("RHEL Release (/etc/redhat-release)", body, changed)

    # --- kernel (uname -a) ---
    la, _ = get_lines(root_a, "sos_commands/kernel/uname_-a", "uname")
    lb, _ = get_lines(root_b, "sos_commands/kernel/uname_-a", "uname")
    body, changed = side_by_side_diff(label_a, label_b, la, lb, width=width)
    report.add("Kernel Version (uname -a)", body, changed)

    # NOTE: hostname/hostnamectl is intentionally NOT compared here — it is
    # certain to differ between any two hosts and isn't a meaningful diff.

    # --- CPU ---
    la, _ = get_lines(root_a, "proc/cpuinfo")
    lb, _ = get_lines(root_b, "proc/cpuinfo")
    body, changed = diff_cpuinfo(label_a, label_b, la, lb)
    report.add("CPU (/proc/cpuinfo)", body, changed)

    # --- Memory ---
    la, _ = get_lines(root_a, "proc/meminfo")
    lb, _ = get_lines(root_b, "proc/meminfo")
    body, changed = diff_meminfo(label_a, label_b, la, lb)
    report.add("Memory (/proc/meminfo)", body, changed)

    # --- Installed RPMs ---
    la, _ = get_lines(root_a, "installed-rpms")
    lb, _ = get_lines(root_b, "installed-rpms")
    body, changed = diff_rpms(label_a, label_b, la, lb)
    report.add("Installed RPMs", body, changed)

    # --- Kernel modules (lsmod) ---
    la, _ = get_lines(root_a, "sos_commands/kernel/lsmod")
    lb, _ = get_lines(root_b, "sos_commands/kernel/lsmod")
    def module_names(lines):
        return sorted({l.split()[0] for l in (lines or [])[1:] if l.strip()})
    ma, mb = module_names(la), module_names(lb)
    if la is None and lb is None:
        body, changed = "_lsmod not present in either report_", False
    elif la is None or lb is None:
        missing = label_a if la is None else label_b
        body, changed = f"_lsmod missing from **{missing}**_", True
    else:
        only_a, only_b = sorted(set(ma) - set(mb)), sorted(set(mb) - set(ma))
        changed = bool(only_a or only_b)
        body = "_identical loaded module set_" if not changed else (
            (f"**Only in {label_a}:** " + ", ".join(only_a) + "\n\n" if only_a else "") +
            (f"**Only in {label_b}:** " + ", ".join(only_b) if only_b else "")
        )
    report.add("Loaded Kernel Modules (lsmod)", body, changed)

    # --- sysctl -a (only differing keys shown) ---
    la, _ = get_lines(root_a, "sos_commands/kernel/sysctl_-a", "sos_commands/sysctl/sysctl_-a")
    lb, _ = get_lines(root_b, "sos_commands/kernel/sysctl_-a", "sos_commands/sysctl/sysctl_-a")
    body, changed = diff_sysctl(label_a, label_b, la, lb)
    report.add("sysctl -a (differing values only)", body, changed)

    # --- SELinux config ---
    la, _ = get_lines(root_a, "etc/selinux/config")
    lb, _ = get_lines(root_b, "etc/selinux/config")
    body, changed = diff_keyvalue(label_a, label_b, la, lb, sep="=")
    report.add("SELinux Config (/etc/selinux/config)", body, changed)

    # --- fstab ---
    la, _ = get_lines(root_a, "etc/fstab")
    lb, _ = get_lines(root_b, "etc/fstab")
    body, changed = side_by_side_diff(label_a, label_b, la, lb, width=width)
    report.add("/etc/fstab", body, changed)

    # --- mounts / findmnt ---
    la, _ = get_lines(root_a, "sos_commands/filesys/findmnt", "sos_commands/filesys/mount_-l")
    lb, _ = get_lines(root_b, "sos_commands/filesys/findmnt", "sos_commands/filesys/mount_-l")
    body, changed = side_by_side_diff(label_a, label_b, la, lb, width=width)
    report.add("Active Mounts (findmnt)", body, changed)

    # --- block devices ---
    la, _ = get_lines(root_a, "sos_commands/block/lsblk")
    lb, _ = get_lines(root_b, "sos_commands/block/lsblk")
    body, changed = side_by_side_diff(label_a, label_b, la, lb, width=width)
    report.add("Block Devices (lsblk)", body, changed)

    # --- disk usage (df) ---
    la, _ = get_lines(root_a, "sos_commands/filesys/df_-al_-x_autofs", "df")
    lb, _ = get_lines(root_b, "sos_commands/filesys/df_-al_-x_autofs", "df")
    body, changed = side_by_side_diff(label_a, label_b, la, lb, width=width)
    report.add("Disk Usage (df)", body, changed)

    # NOTE: network interfaces (ip address) and the routing table are
    # intentionally NOT compared here — IPs/routes are certain to differ
    # between any two hosts and aren't a meaningful diff.

    # --- firewalld ---
    la, _ = get_lines(root_a, "sos_commands/firewalld/firewall-cmd_--list-all-zones")
    lb, _ = get_lines(root_b, "sos_commands/firewalld/firewall-cmd_--list-all-zones")
    body, changed = side_by_side_diff(label_a, label_b, la, lb, width=width)
    report.add("Firewalld Zones (firewall-cmd --list-all-zones)", body, changed)

    # --- enabled/disabled services ---
    la, _ = get_lines(root_a, "sos_commands/systemd/systemctl_list-unit-files")
    lb, _ = get_lines(root_b, "sos_commands/systemd/systemctl_list-unit-files")
    body, changed = diff_service_list(label_a, label_b, la, lb)
    report.add("Systemd Unit Enablement", body, changed)

    # NOTE: collection date/time is intentionally NOT compared here — two
    # sosreports are essentially never captured at the same instant, so
    # this always "differs" and isn't a meaningful diff.

    # --- Running processes ---
    la, _ = get_lines(root_a, "sos_commands/process/ps_auxfwww", "sos_commands/process/ps_auxwwwm",
                       "sos_commands/process/ps_alxwww", "sos_commands/process/ps_-elfL")
    lb, _ = get_lines(root_b, "sos_commands/process/ps_auxfwww", "sos_commands/process/ps_auxwwwm",
                       "sos_commands/process/ps_alxwww", "sos_commands/process/ps_-elfL")
    body, changed = diff_process_set(label_a, label_b, la, lb)
    report.add("Running Processes", body, changed)

    # --- Listening sockets ---
    entries_a, source_a = get_listening_sockets(root_a)
    entries_b, source_b = get_listening_sockets(root_b)
    body, changed = diff_listening_sockets(label_a, label_b, entries_a, entries_b, source_a, source_b)
    report.add("Listening Sockets (TCP/UDP)", body, changed)

    return report


def main():
    ap = argparse.ArgumentParser(description="Compare two RHEL 9 sosreport archives/directories.")
    ap.add_argument("report_a", help="First sosreport (.tar.xz/.tar.gz or extracted dir)")
    ap.add_argument("report_b", help="Second sosreport (.tar.xz/.tar.gz or extracted dir)")
    ap.add_argument("-o", "--output", default="sosreport-comparison.md",
                     help="Path to write the Markdown report (default: %(default)s)")
    ap.add_argument("-W", "--width", type=int, default=DIFF_WIDTH,
                     help="Total width of side-by-side diff output, like `diff -y -W` "
                          "(default: %(default)s)")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp_base:
        root_a = prepare_root(args.report_a, tmp_base)
        root_b = prepare_root(args.report_b, tmp_base)

        label_a = hostname_of(root_a)
        label_b = hostname_of(root_b)
        if label_a == label_b:
            label_a, label_b = f"{label_a} (A)", f"{label_b} (B)"

        report = compare(root_a, root_b, label_a, label_b, width=args.width)
        rendered = report.render()

        with open(args.output, "w") as f:
            f.write(rendered)

        changed_count = sum(1 for _, _, c in report.sections if c)
        print(f"Compared {len(report.sections)} artifact sections; "
              f"{changed_count} differ. Report written to: {args.output}")


if __name__ == "__main__":
    main()
