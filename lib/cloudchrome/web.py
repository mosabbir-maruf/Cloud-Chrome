"""websockify and the noVNC assets.

The only port that is reachable from outside the container is the noVNC
WebSocket port. It is bound to the container's network namespace and the
Codespaces port forwarding keeps it private.

The VNC server itself listens on the loopback interface only, so the raw RFB
protocol cannot be reached from outside even if the forwarded port were
exposed.
"""

import os


def websockify_arguments(port, vnc_port, novnc_root, bind_address="0.0.0.0"):
    """The argument vector of the WebSocket-to-TCP bridge.

    ``--web`` serves the noVNC client from the image, so the browser gets a
    working client without downloading anything.
    """
    return [
        "websockify",
        "--web=%s" % novnc_root,
        "%s:%d" % (bind_address, port),
        "127.0.0.1:%d" % vnc_port,
    ]


def novnc_index_present(novnc_root):
    """True when the noVNC client is installed in the image."""
    return os.path.isfile(os.path.join(novnc_root, "vnc.html"))


def check_rfb_is_loopback_only(vnc_port=5900):
    """Verify that nothing but the loopback interface listens on the RFB port.

    The check reads the socket table instead of trusting the arguments the
    server was started with.
    """
    listening = _listening_addresses(vnc_port)
    if not listening:
        return {"listening": False, "addresses": []}

    loopback = {"0100007F", "00000000000000000000000001000000"}
    non_loopback = [item for item in listening if item.upper() not in loopback]
    return {
        "listening": True,
        "addresses": listening,
        "non_loopback": len(non_loopback),
    }


def http_port_is_bound(port=6080):
    """True when something is listening on the noVNC port."""
    return _listening_addresses(port) != []


def _listening_addresses(port):
    """Return the local addresses on which *port* is in the LISTEN state."""
    addresses = []
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":
                continue
            address, _, port_hex = fields[1].rpartition(":")
            try:
                bound = int(port_hex, 16)
            except ValueError:
                continue
            if bound == port:
                addresses.append(address)
    return addresses
