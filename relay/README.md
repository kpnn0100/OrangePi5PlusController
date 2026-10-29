# Device manager (relay) - operator guide

The device manager is a small server on the internet that machines running Arstro Remote
advertise themselves to. You open `https://<server>/arstro/internal/devices/` in a browser
(or use the agent client), pick a machine, give its access key and a port, and reach that
port of the machine. Traffic between you and the machine is end-to-end encrypted with the
machine's access key: the device manager only forwards ciphertext and never has a key.

How it works and the security model: [docs/relay/RELAY.md](../docs/relay/RELAY.md). Wire
format: [docs/relay/PROTOCOL.md](../docs/relay/PROTOCOL.md). Agents: [docs/relay/AGENT_API.md](../docs/relay/AGENT_API.md).

It is one Python process (standard library only) in a Docker image, listening on port 8090
with plain HTTP. For real use, put HTTPS in front of it - the included Docker Compose file
does that with Caddy and a free Let's Encrypt certificate.

> **The browser viewer needs https, or `http://localhost`.** Browsers only allow service
> workers and WebCrypto on a secure context. Over plain `http://<lan-ip>:8090` the device
> list works but opening a machine's page does not. The agent client works over plain http
> (still end-to-end encrypted), but use https on the internet anyway.

## 1. Quick start: local or LAN test (`docker run`)

From the root of this repository (the build needs files from `server/` too):

```sh
docker build -f relay/Dockerfile -t arstro-devices .
docker run -d --name arstro-devices --restart unless-stopped \
  -p 8090:8090 -v arstro-devices:/data arstro-devices
docker logs arstro-devices          # shows "devices join with token: ..."
```

Open <http://localhost:8090/arstro/internal/devices/> on the same computer. Point a machine
at `http://<this-computer's-ip>:8090/arstro/internal/devices` with the join token (on the
machine: the *Internet access* page, `arstro-remote relay set --url ... --token ... --enable`,
or `install.sh --relay <url> --relay-token <token>`). It appears in the list within seconds.

## 2. On a server with HTTPS (Docker Compose + Caddy), e.g. Oracle Cloud

You need a domain name (or a free sub-domain from a dynamic DNS service) whose **A record
points at the server's public IP**, and ports **80 and 443 open** (Let's Encrypt checks the
domain over them).

```sh
git clone <this repository> arstro && cd arstro
DOMAIN=devices.example.com docker compose -f relay/docker-compose.yml up -d
docker compose -f relay/docker-compose.yml logs devices        # the join token
```

The list is at `https://devices.example.com/arstro/internal/devices/`; machines use that URL
(without the trailing `/`) and the join token. Anything else on the domain answers 404.

Optional settings go in front of the command or into `relay/.env` next to the compose file
(`RELAY_PASSWORD=...`, `RELAY_NAME=...`, `RELAY_JOIN_TOKEN=...`, `RELAY_BASE=...`).

Update to a newer version: `git pull` and the same `docker compose ... up -d --build`.
The devices, the join token and the certificates are kept in the named volumes.

### Oracle Cloud notes

* **Open the ports twice.** In the VCN's *security list* (or network security group) add
  ingress rules for TCP 80 and 443 (and UDP 443 for HTTP/3) from `0.0.0.0/0`, **and** open
  them in the VM's own firewall - Oracle's Ubuntu images block them with iptables:
  ```sh
  sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
  sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
  sudo netfilter-persistent save
  ```
  (Oracle Linux: `sudo firewall-cmd --permanent --add-service=http --add-service=https && sudo firewall-cmd --reload`.)
* **Ampere (A1) VMs are arm64.** The image is built from `python:3.12-slim`, which is
  multi-arch: build it on the VM and it is an arm64 image, nothing to change.
* Docker on Ubuntu: `sudo apt install docker.io docker-compose-v2` (or Docker's own packages),
  then `sudo usermod -aG docker $USER` and log in again.
* The always-free shapes are plenty: the relay forwards bytes and uses a few MB of memory.

### Your own reverse proxy instead of Caddy

Run the container as in section 1 (publish `-p 127.0.0.1:8090:8090` so only the proxy
reaches it), set `-e RELAY_TRUST_PROXY=1`, and proxy `/arstro/internal/devices*` to it with
WebSocket upgrades allowed and `X-Forwarded-For` / `X-Forwarded-Proto` set. nginx:

```nginx
location /arstro/internal/devices {
    proxy_pass http://127.0.0.1:8090;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 1h;
    client_max_body_size 1m;
}
```

**A proxy of your own instead of Caddy** (nginx already on 443): start only the device
manager, `RELAY_LISTEN=127.0.0.1:8090 docker compose -f relay/docker-compose.yml up -d devices`
(`0.0.0.0:8090` when the proxy is in Docker or on another server), and proxy
`https://<domain>` to it with WebSockets on and long timeouts - the nginx block is in
[kit/README.md](kit/README.md). In the kit this is `PROXY=own` in `.env`.

## 2b. A kit for someone who only has Docker (no repository)

```
relay/package.sh --arm64-host user@arm64-machine     # e.g. the Orange Pi (Docker installed)
```

builds the image for amd64 here and for arm64 natively on that machine over SSH (Oracle's
free Ampere VMs are arm64), and writes `release/arstro-devices-<version>.tar.gz` - send that
one file. It holds both images (`docker save`, gzip, with `SHA256SUMS`), an image-only
`docker-compose.yml` generated from this folder's, the `Caddyfile`, `.env.example`, `run.sh` and
a README for the person running it (`relay/kit/README.md`: Oracle ports, start, token, update).
They run `cp .env.example .env` (set `DOMAIN`), `./run.sh`, `./run.sh token`.
`release/` is not committed; `--no-amd64` skips the local build.

## 3. Settings

All settings are environment variables (`docker run -e NAME=value`, or the compose file).

| Variable | Default | What |
|---|---|---|
| `RELAY_HOST` | `0.0.0.0` | address to listen on |
| `RELAY_PORT` | `8090` | port to listen on (plain HTTP) |
| `RELAY_BASE` | `/arstro/internal/devices` | base path of every page and API; starts with `/`, no trailing `/` (`/` = the root). With compose, Caddy follows it |
| `RELAY_DATA` | `/data` | data directory (the volume): devices, join token, cookie key |
| `RELAY_JOIN_TOKEN` | *(generated)* | the token every machine needs to join. Unset: generated once into `<data>/join_token` and printed in the log at every start |
| `RELAY_PASSWORD` | *(none)* | password for the device list (cookie login, or `Authorization: Bearer <password>` for scripts). Unset: the list is open - anyone who finds the page sees the machines' names (never a way in: each machine still needs its access key) |
| `RELAY_TRUST_PROXY` | `0` | `1` behind your own reverse proxy: the client address is the first `X-Forwarded-For` entry and `X-Forwarded-Proto: https` marks the login cookie `Secure`. Never set it when clients reach the relay directly - they could fake their address |
| `RELAY_NAME` | `Devices` | title of the list page |
| `RELAY_CLIENT_DIR` | the image's client files | where `B/client/` files come from (`client.py`, `e2e.py`, `ws.py`); outside Docker: `server/arstro_remote/relay` of this repository |

Tuning (the defaults are right for normal use; the tests change them):

| Variable | Default | What |
|---|---|---|
| `RELAY_RATE_LIMIT` | `30/10` | new WebSocket connections, logins and wrong Bearer passwords allowed per client address: `count/seconds`; more get HTTP 429 |
| `RELAY_HEARTBEAT` | `20` | seconds between a machine's pings (sent to it in `welcome`); 3 silent heartbeats drop it |
| `RELAY_PENDING` | `10` | seconds a viewer waits for the machine's data connection |
| `RELAY_STATIC` | the package's `static/` | where the pages come from |

Fixed limits (docs/relay/PROTOCOL.md section 6): 1000 registered machines, 32 viewer channels
per machine, 1 MiB per WebSocket message (a larger one closes the socket with code 1009).

## 4. Join token and admin commands

The join token keeps strangers from adding machines. Find it:

```sh
docker logs arstro-devices 2>&1 | grep "join with token"
docker exec arstro-devices python -m arstro_relay admin token
```

Admin commands work on the data directory; a running device manager picks changes up within
a few seconds (no restart). With compose use `docker compose -f relay/docker-compose.yml exec devices ...`.

| Command | What |
|---|---|
| `docker exec arstro-devices python -m arstro_relay admin list` | registered machines: id, name, hostname, platform, version, slot, last seen (`--json` for scripts) |
| `docker exec arstro-devices python -m arstro_relay admin forget <id>` | remove a machine: it is disconnected (it gets `bye`), and its id is free again - the next machine joining with that id owns it |
| `docker exec arstro-devices python -m arstro_relay admin token` | print the join token |
| `docker exec arstro-devices python -m arstro_relay admin token --rotate` | make a new join token. Machines online now stay connected; every machine needs the new token for its next connection. Not possible when the token comes from `RELAY_JOIN_TOKEN` (change the variable and restart instead) |

To keep a machine out for good: `forget` it **and** rotate the join token (then give the new
token only to the machines that should stay).

Each machine id is bound to the machine's secret the first time it joins: another machine
cannot take an id that is in use ("this device id belongs to another device"). A machine
that was reinstalled with a new secret must be `forget`-ten first.

## 5. Data, logs, backup

The volume (`/data`) holds:

| File | What |
|---|---|
| `devices.json` | the registered machines: name, hostname, platform, version, slot, web port, first / last seen and `sha256` of each machine's secret (never the secret) |
| `join_token` | the generated join token (mode 0600) |
| `cookie_key` | random key for the login cookie (deleting it logs every browser out) |

Back it up with `docker run --rm -v arstro-devices:/data -v "$PWD":/backup busybox tar czf /backup/devices.tgz -C /data .`.
Losing it is not a disaster: machines re-register on their next connection (with a new
generated token you must give them the new token).

Logs go to stderr (`docker logs`): `time level component: message`. They record machines
coming online / going offline (id, name, address), viewer channels (machine, viewer address,
bytes each way, duration) and every refusal with its reason. They never contain access keys,
machine secrets, passwords or forwarded content; the join token appears only in the start-up
line when it was generated.

## 6. Without Docker

Any Linux with Python 3.10+:

```sh
cd relay
RELAY_DATA=$HOME/.arstro-devices RELAY_PORT=8090 python3 -m arstro_relay
RELAY_DATA=$HOME/.arstro-devices python3 -m arstro_relay admin list
```

`B/client/` then serves the client files from `server/arstro_remote/relay` of this checkout.

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| A machine never shows up | wrong URL (it must end in the base path, no `/`), wrong join token (`device refused ...: wrong join token` in the log), or the machine cannot reach the server (ports, firewall) |
| "this device id belongs to another device" | two machines use the same id, or the machine was reinstalled: `admin forget <id>` |
| The list loads but a machine's page does not | not a secure context: use https (or localhost) |
| Caddy cannot get a certificate | DNS does not point at the server yet, or 80/443 are closed (security list **and** VM firewall) |
| HTTP 429 | more than 30 new connections in 10 s from one address; wait a few seconds |

## 8. Tests

```sh
python3 relay/tests/test_relay_server.py        # the server: routes, registry, forwarding, limits, admin
```
