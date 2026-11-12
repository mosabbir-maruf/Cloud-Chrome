"""The measurements the performance probe takes.

Every function returns a dictionary, and every function says when a metric
is not available instead of guessing a number. A measurement that could not
be taken is reported as such, because a missing metric and a metric of
zero do not mean the same thing.

The module is used by ``tests/probe/performance-probe.sh``. It only reads
the system: it never touches the Cloud-Chrome state.
"""

import http.client
import os
import socket
import time


# ---------------------------------------------------------------------------
# the memory the container accounts for
# ---------------------------------------------------------------------------
def _cgroup_locations():
    """The cgroup directories of this process.

    The unified hierarchy is used when the kernel offers it, because the
    numbers it reports are the ones the runtime is charged for.
    """
    unified = None
    legacy_memory = None
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as handle:
            for line in handle:
                parts = line.strip().split(":")
                if len(parts) != 3:
                    continue
                _, controllers, path = parts
                if controllers == "":
                    unified = path
                elif "memory" in controllers.split(","):
                    legacy_memory = path
    except OSError:
        pass
    return unified, legacy_memory


def _read_int(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def cgroup_memory():
    """The memory the cgroup accounts for.

    cgroup v2 is preferred and cgroup v1 is the fallback. When neither is
    readable the result says so, instead of reporting the memory of the
    whole machine.
    """
    unified, legacy = _cgroup_locations()

    if unified is not None:
        root = "/sys/fs/cgroup%s" % unified
        current = _read_int(os.path.join(root, "memory.current"))
        if current is not None:
            stat = {}
            try:
                with open(
                    os.path.join(root, "memory.stat"), encoding="utf-8"
                ) as handle:
                    for line in handle:
                        key, _, value = line.partition(" ")
                        try:
                            stat[key.strip()] = int(value.strip())
                        except ValueError:
                            continue
            except OSError:
                pass
            return {
                "source": "cgroup v2",
                "path": root,
                "current_bytes": current,
                "peak_bytes": stat.get("peak"),
                "anonymous_bytes": stat.get("anon"),
                "file_bytes": stat.get("file"),
                "swap_bytes": stat.get("swap"),
                "available": True,
            }

    if legacy is not None:
        root = "/sys/fs/cgroup/memory%s" % legacy
        current = _read_int(os.path.join(root, "memory.usage_in_bytes"))
        if current is not None:
            return {
                "source": "cgroup v1",
                "path": root,
                "current_bytes": current,
                "peak_bytes": _read_int(
                    os.path.join(root, "memory.max_usage_in_bytes")
                ),
                "available": True,
            }

    return {
        "source": None,
        "path": None,
        "current_bytes": None,
        "peak_bytes": None,
        "available": False,
        "note": "no readable cgroup memory controller was found",
    }


# ---------------------------------------------------------------------------
# the memory the processes use
# ---------------------------------------------------------------------------
def _pss_of(pid):
    """The proportional set size of a process, in bytes.

    ``smaps_rollup`` is used when the kernel offers it, and ``smaps`` is
    the fallback. The result is None when neither can be read, which
    happens when the kernel has the accounting disabled.
    """
    rollup = "/proc/%s/smaps_rollup" % pid
    try:
        with open(rollup, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("Pss:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/%s/smaps" % pid, encoding="utf-8") as handle:
            total = 0
            for line in handle:
                if line.startswith("Pss:"):
                    total += int(line.split()[1]) * 1024
            return total
    except (OSError, ValueError):
        return None


def _rss_of(pid):
    try:
        with open("/proc/%s/statm" % pid, encoding="utf-8") as handle:
            fields = handle.read().split()
        return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def process_memory(pids):
    """The memory of the given processes.

    The proportional set size is the measure that can be summed without
    counting the shared pages once per process, so it is the number the
    report uses. The resident set size is reported as well, because it is
    the number the kernel's own accounting uses.
    """
    processes = []
    total_pss = 0
    total_rss = 0
    pss_known = True
    for pid in pids:
        pss = _pss_of(pid)
        rss = _rss_of(pid)
        if pss is None:
            pss_known = False
        else:
            total_pss += pss
        if rss:
            total_rss += rss
        processes.append({"pid": pid, "pss_bytes": pss, "rss_bytes": rss})
    return {
        "processes": processes,
        "total_pss_bytes": total_pss if pss_known else None,
        "total_rss_bytes": total_rss,
        "pss_available": pss_known,
    }


# ---------------------------------------------------------------------------
# the CPU the container uses
# ---------------------------------------------------------------------------
def cpu_snapshot():
    """The CPU time counters of the machine.

    The values are the jiffies of ``/proc/stat``. The difference between
    two snapshots, divided by the elapsed wall time, is the share of the
    machine that was busy.
    """
    try:
        with open("/proc/stat", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("cpu "):
                    fields = [int(value) for value in line.split()[1:]]
                    return {
                        "user": fields[0],
                        "nice": fields[1],
                        "system": fields[2],
                        "idle": fields[3],
                        "iowait": fields[4] if len(fields) > 4 else 0,
                        "irq": fields[5] if len(fields) > 5 else 0,
                        "softirq": fields[6] if len(fields) > 6 else 0,
                        "steal": fields[7] if len(fields) > 7 else 0,
                        "total": sum(fields),
                        "available": True,
                    }
    except (OSError, ValueError, IndexError):
        pass
    return {"available": False}


def process_cpu_seconds(pids):
    """The CPU time the given processes have used, in seconds.

    The value is the sum of the user and the system time of the
    processes, read from ``/proc/<pid>/stat``. It is the CPU the
    processes themselves used, which is the number that says how much the
    desktop costs while it is idle.

    It is measured this way, and not from ``/proc/stat``, because inside a
    container ``/proc/stat`` holds the counters of the **host**, which
    includes every other tenant of the machine. That number moves with
    the load of the machine and says nothing about the desktop.
    """
    ticks = os.sysconf("SC_CLK_TCK")
    total = 0
    known = True
    for pid in pids:
        try:
            with open("/proc/%s/stat" % pid, encoding="utf-8") as handle:
                fields = handle.read().rsplit(")", 1)[1].split()
            # The fields after the command name: state is the first, and
            # the user and the system time are the twelfth and the
            # thirteenth.
            total += int(fields[11]) + int(fields[12])
        except (OSError, ValueError, IndexError):
            known = False
    if not known and not pids:
        return None
    return total / float(ticks)


def cpu_usage_between(first, second, elapsed):
    """The share of the machine that was busy between two snapshots."""
    if not first.get("available") or not second.get("available"):
        return None
    total = second["total"] - first["total"]
    if total <= 0 or elapsed <= 0:
        return None
    idle = (second["idle"] - first["idle"]) + (second["iowait"] - first["iowait"])
    busy = total - idle
    return {
        "busy_jiffies": busy,
        "total_jiffies": total,
        "busy_fraction": busy / float(total),
        "wall_seconds": elapsed,
    }


# ---------------------------------------------------------------------------
# the noVNC client and the RFB server
# ---------------------------------------------------------------------------
def http_latency(host="127.0.0.1", port=6080, path="/", samples=10, timeout=10.0):
    """How long the noVNC client takes to answer, in milliseconds.

    The measurement is the time of a complete request and response, which
    is what a browser waits for. A successful response says that the
    client serves the page; it says nothing about what the page shows.
    """
    timings = []
    failures = 0
    last_status = None
    for _index in range(samples):
        connection = None
        try:
            started = time.monotonic()
            connection = http.client.HTTPConnection(host, port, timeout=timeout)
            connection.request("GET", path)
            response = connection.getresponse()
            response.read()
            elapsed = (time.monotonic() - started) * 1000.0
            last_status = response.status
            if response.status == 200:
                timings.append(elapsed)
            else:
                failures += 1
        except (OSError, http.client.HTTPException):
            failures += 1
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
    return {
        "samples_ms": timings,
        "failures": failures,
        "status": last_status,
        "url": "http://%s:%d%s" % (host, port, path),
    }


def rfb_handshake(host="127.0.0.1", port=5900, timeout=5.0):
    """The RFB handshake, up to the authentication step.

    The server's protocol version is read, the same version is sent back,
    and the security types the server offers are read. The handshake is
    complete when the security types have been read.

    This says that the RFB port speaks the protocol and what
    authentication it requires. It says nothing about whether a browser can
    draw the desktop: that needs a human.
    """
    result = {
        "host": host,
        "port": port,
        "version": None,
        "security_types": [],
        "completed": False,
        "error": None,
    }
    connection = None
    try:
        connection = socket.create_connection((host, port), timeout=timeout)
        connection.settimeout(timeout)
        version = connection.recv(12)
        if len(version) != 12:
            result["error"] = "the server sent %d bytes, not 12" % len(version)
            return result
        result["version"] = version.decode("ascii", "replace").strip()
        connection.sendall(version)
        count = connection.recv(1)
        if not count:
            result["error"] = "the server sent no security types"
            return result
        number = count[0]
        if number == 0:
            reason = connection.recv(4)
            result["error"] = "the server refused the connection"
            return result
        types = b""
        while len(types) < number:
            chunk = connection.recv(number - len(types))
            if not chunk:
                break
            types += chunk
        result["security_types"] = list(types)
        result["completed"] = True
    except (OSError, socket.timeout) as exc:
        result["error"] = str(exc)
    finally:
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
    return result


def machine_facts():
    """What the machine is, for the report."""
    facts = {}
    try:
        facts["cpu_count"] = os.cpu_count()
    except Exception:  # noqa: BLE001
        facts["cpu_count"] = None
    try:
        facts["page_size"] = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError):
        facts["page_size"] = None
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    facts["mem_total_bytes"] = int(line.split()[1]) * 1024
                    break
    except (OSError, ValueError):
        facts["mem_total_bytes"] = None
    facts["load_average"] = os.getloadavg()
    return facts
