import ipaddress, socket, threading, sys

LISTEN = ("0.0.0.0", 9334)   # 0.0.0.0 because WSL's NAT address changes; allowed() decides who gets through
TARGET = ("127.0.0.1", 9333)
WSL_NAT = ipaddress.ip_network("172.16.0.0/12")


def allowed(client):
    """Only this PC and WSL may reach Chrome's debug port: a CDP client controls every logged-in site.
    A WSL connection arrives on the Windows side of the WSL NAT; a LAN device arrives on the LAN address."""
    peer = ipaddress.ip_address(client.getpeername()[0])
    local = ipaddress.ip_address(client.getsockname()[0])
    return peer.is_loopback or (peer in WSL_NAT and local in WSL_NAT)

def pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    finally:
        try: a.shutdown(socket.SHUT_RD)
        except OSError: pass
        try: b.shutdown(socket.SHUT_WR)
        except OSError: pass

def handle(client):
    try:
        if not allowed(client):
            print(f"forwarder: refused {client.getpeername()[0]}", flush=True)
            client.close()
            return
    except OSError:
        client.close()
        return
    try:
        upstream = socket.create_connection(TARGET, timeout=5)
    except OSError as e:
        client.close()
        return
    # The 5 s is for connecting only. Left on, it cut every CDP call that waits
    # longer than 5 s for Chrome to answer (an awaited Runtime.evaluate), 2026-10-05.
    upstream.settimeout(None)
    threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
    pipe(upstream, client)
    client.close(); upstream.close()

def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Single-instance guard. On Windows, SO_REUSEADDR lets a second socket steal
    # the same port (SO_REUSEADDR here == hijack), which stacked six forwarders on
    # 9334. SO_EXCLUSIVEADDRUSE is the Windows opposite: a second instance fails to
    # bind and we exit fast instead of silently stacking.
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    except (AttributeError, OSError):
        pass  # non-Windows / option unavailable — bind() below is still the gate
    try:
        s.bind(LISTEN)
    except OSError as e:
        print(f"forwarder: port {LISTEN[1]} already in use ({e}); exiting", flush=True)
        sys.exit(1)
    s.listen(64)
    print(f"forwarder {LISTEN} -> {TARGET}", flush=True)
    while True:
        c, _ = s.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()

if __name__ == "__main__":
    main()
