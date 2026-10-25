"""Finding this computer's address, so people know what to type into a phone."""
import socket


def computer_name():
    return socket.gethostname()


def local_addresses():
    """IPv4 addresses of this computer that other devices on the same network could use."""
    found = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("10.255.255.255", 1))  # sends nothing; shows which network card is in use
            found.append(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    unique = []
    for ip in found:
        if ip not in unique and not ip.startswith(("127.", "169.254.")) and ip != "0.0.0.0":
            unique.append(ip)
    return unique
