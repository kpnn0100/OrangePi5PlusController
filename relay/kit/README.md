# Arstro device manager @VERSION@ - how to run it

A small web service that lets the owner reach their machines over the internet. Machines
connect *out* to it; it lists them and passes encrypted traffic between a browser and a
machine. It cannot read that traffic: it is end-to-end encrypted with keys only the owner
has. It needs no database and no configuration beyond a domain name.

## What you need

* A Linux server with a public address (an Oracle Cloud VM is fine, arm64 or x86_64).
* Docker with the compose plugin: <https://docs.docker.com/engine/install/> (on Ubuntu:
  `sudo apt install docker.io docker-compose-v2`, or Docker's own packages).
* A domain name pointing at the server (an `A` record), e.g. `devices.example.com`.
* Ports **80 and 443 open** - on Oracle Cloud in **two** places:
  1. the VCN **security list** (Networking › Virtual Cloud Networks › your VCN › Security
     Lists › Add Ingress Rules: source `0.0.0.0/0`, TCP, destination ports `80` and `443`);
  2. the VM's own firewall. Oracle's Ubuntu images block everything but SSH:
     ```
     sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
     sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
     sudo netfilter-persistent save
     ```
     (Oracle Linux: `sudo firewall-cmd --permanent --add-service=http --add-service=https && sudo firewall-cmd --reload`.)

## Start it

```
tar xzf arstro-devices-@VERSION@.tar.gz
cd arstro-devices-@VERSION@
cp .env.example .env          # set DOMAIN=your.domain (and optionally RELAY_PASSWORD)
./run.sh                      # loads the right image for this CPU, starts it with HTTPS
./run.sh token                # the join token - send it to the owner
```

The device list is then at `https://<DOMAIN>/arstro/internal/devices/`. The first start gets a
Let's Encrypt certificate by itself (Caddy); that needs the domain and both ports right.

Other commands: `./run.sh status` (containers + registered machines), `./run.sh logs`,
`./run.sh stop`. Everything it keeps (the join token, the list of machines, the certificate)
is in Docker volumes and survives restarts and updates. It restarts by itself after a reboot.

## Behind your own nginx (ports 80/443 stay yours)

In `.env` set `PROXY=own`, `DOMAIN=` the name your nginx serves, and `RELAY_LISTEN`
(`127.0.0.1:8090` if nginx runs directly on this server, `0.0.0.0:8090` if it runs in Docker or
on another server). `./run.sh` then starts only the device manager - no Caddy. In nginx:

```nginx
# http { } block, once
map $http_upgrade $connection_upgrade { default upgrade; '' close; }

server {
    listen 443 ssl;
    server_name arstro.example.com;            # your DOMAIN; your certificate lines as usual
    location / {
        proxy_pass http://127.0.0.1:8090;      # RELAY_LISTEN as nginx reaches it, no path after it
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;              # WebSockets: required
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 1h;                 # connections stay open for long
        proxy_send_timeout 1h;
        proxy_buffering off;
    }
}
```

`proxy_pass` from nginx in Docker on this server: the Docker host's address, i.e. the `docker0`
address (`ip -4 addr show docker0`) with port 8090;
from another server: this server's private address. Everything - pages, terminals, screens,
file uploads and downloads of any size - travels inside the WebSockets, so nginx's
`client_max_body_size` does not matter. The device list is at `https://<DOMAIN>/arstro/internal/devices/`.

## Updating

Unpack the new kit next to the old one, copy your `.env` into it, `./run.sh stop` in the old
folder and `./run.sh` in the new one. The volumes (token, machines, certificate) carry over.

## Good to know

* Machines join with the join token. Rotate it with
  `docker compose exec devices python -m arstro_relay admin token --rotate` (machines already
  in the list stay; new ones need the new token). Remove a machine:
  `... admin forget <id>` (rotate the token too, or it can join again).
* Please run the files of this kit unchanged: the pages it serves do the encryption in the
  owner's browser. `SHA256SUMS` lists the image checksums; `run.sh` checks them.
* Logs show machines coming and going and viewer addresses, never traffic content.
