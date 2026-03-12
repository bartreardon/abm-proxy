# HTTPS / TLS Setup

ABM Proxy itself speaks plain HTTP. For HTTPS you place a reverse proxy in front of it.
The two most common options are **Nginx** and **Caddy**.

> **Why not terminate TLS in the app?**
> Gunicorn (the production WSGI server) is not designed to handle TLS directly.
> A reverse proxy handles TLS termination, certificate renewal, and connection buffering,
> while the app continues to listen on a local HTTP port.

---

## Option A — Nginx + Certbot (Let's Encrypt)

**Prerequisites:** a public DNS A record pointing to your server and port 443 open.

### 1 – Install Nginx and Certbot

```bash
sudo apt install nginx certbot python3-certbot-nginx
```

### 2 – Create a site config

Create `/etc/nginx/sites-available/abm-proxy`:

```nginx
server {
    listen 80;
    server_name abm.example.com;   # replace with your domain

    location / {
        proxy_pass         http://127.0.0.1:5050;   # match your PORT
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/abm-proxy /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

### 3 – Obtain a certificate

```bash
sudo certbot --nginx -d abm.example.com
```

Certbot rewrites the Nginx config to add TLS, and installs a cron/systemd timer for
automatic renewal. Your service is then reachable at `https://abm.example.com`.

---

## Option B — Caddy (automatic HTTPS)

Caddy obtains and renews Let's Encrypt certificates automatically with no extra tooling.

**Prerequisites:** same as above — a public DNS record and port 443 open.

### 1 – Install Caddy

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy
```

### 2 – Configure the Caddyfile

Edit `/etc/caddy/Caddyfile`:

```caddy
abm.example.com {                   # replace with your domain
    reverse_proxy localhost:5050    # match your PORT
}
```

```bash
sudo systemctl reload caddy
```

Caddy handles certificate issuance and renewal automatically.

---

## Internal / self-signed certificates

If the service is internal only (no public DNS), you can use a self-signed certificate
or your organisation's internal CA:

```bash
# Generate a self-signed cert (valid 10 years)
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem \
  -days 3650 -nodes -subj "/CN=abm-proxy"
```

Pass the certificate and key to Nginx or Caddy using their respective `ssl_certificate` /
`tls` directives. Clients will need to trust your internal CA or accept the self-signed cert.

---

## Binding the app to localhost only

Once TLS termination is handled by a reverse proxy, restrict the app to loopback so it
cannot be reached directly over the network. Set in `.env`:

```ini
HOST=127.0.0.1
PORT=5050
```

The reverse proxy forwards traffic; direct access to port 5050 is then blocked at the
network level.
