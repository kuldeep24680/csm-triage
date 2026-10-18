# EC2 server setup (for the Docker deployment)

This gets one EC2 server ready to run Triage with Docker Compose. It covers the AWS side and the host only: instance, network, Docker, swap and repo access. Starting the app is in [DEPLOYMENT.md](DEPLOYMENT.md).

Allow about 30 minutes. Each step ends with a **Check**.

---

## What the server has to run

| Component | Need |
|---|---|
| Postgres 16 (container) | About 1.2M rows of synthetic data plus indexes: ~2–3 GB on disk, 0.5–1 GB RAM |
| API + web app (container) | uvicorn; light, ~300 MB RAM |
| Nightly agent runs (scheduler container) | pandas loads a tenant's history into memory: peaks around 2–3 GB for the largest tenant |
| One-time model training | The heaviest step: ~3 min, peaks around 3–4 GB RAM |
| Image build (on the server) | `npm` and `pip` builds need ~2 GB RAM and ~5 GB disk for layers and cache |
| Caddy (container) | TLS on ports 80 and 443 |

Traffic is tiny (a few users), so **memory is the constraint, not CPU**.

---

## Machine configuration

| Setting | Pick | Why |
|---|---|---|
| **Instance type** | **`t3.large`** (2 vCPU, 8 GB RAM, x86_64) | 8 GB covers training plus Postgres plus API at the same time, with headroom. Burstable CPU is fine: the CPU is idle most of the day and busy only during nightly runs |
| Cheaper alternative | `t4g.large` (ARM, 8 GB) | About 20% cheaper. All our images work on ARM, but x86 has the fewest surprises |
| Smallest that works | `t3.medium` (4 GB) **+ 4 GB swap** | Possible after the initial load and training; tight while building images. Not recommended for the first setup |
| **AMI** | **Ubuntu Server 24.04 LTS**, 64-bit (x86) | Docker's official packages support it; 24.04 is the long-term release |
| **Storage** | **30 GB gp3** root volume | OS ~3 GB, Docker images ~4 GB, data and DB ~4 GB, build cache and logs; room to grow |
| **Region** | `ap-south-1` (Mumbai) | Closest to you; any region works |
| Approx. cost | ~$0.08/hour, about $60–65/month left running | Stop the instance when not demoing (see the end) |

---

## 1. Set a budget alert (2 minutes, recommended)

Go to **Billing → Budgets → Create budget → Monthly cost budget**. Set the amount to **$30** and an email alert at 80%.

**Check:** the budget appears in the list.

---

## 2. Create a key pair

Go to **EC2 → Key pairs → Create key pair**. Name it `triage-key`, type **ED25519**, format **.pem**. Download it, then:

```bash
mv ~/Downloads/triage-key.pem ~/.ssh/ && chmod 400 ~/.ssh/triage-key.pem
```

**Check:** `ls -l ~/.ssh/triage-key.pem` shows `-r--------`.

---

## 3. Create the security group

Go to **EC2 → Security groups → Create security group**. Name it `triage-sg`, in the default VPC.

| Type | Port | Source | Why |
|---|---|---|---|
| SSH | 22 | **My IP** | Admin access. Never open to 0.0.0.0/0 |
| HTTP | 80 | 0.0.0.0/0 | Let's Encrypt validation and redirect to HTTPS |
| HTTPS | 443 | 0.0.0.0/0 | The app |

Leave outbound as the default (all allowed). The server needs it for GitHub, Docker Hub, OpenAI and Let's Encrypt.

Do **not** open 5432 (Postgres) or 8000 (API). They stay inside Docker.

**Check:** the group shows exactly 3 inbound rules.

---

## 4. Launch the instance

Go to **EC2 → Launch instance**:

| Field | Value |
|---|---|
| Name | `triage-prod` |
| AMI | Ubuntu Server 24.04 LTS (x86) |
| Instance type | `t3.large` |
| Key pair | `triage-key` |
| Network settings → Select existing security group | `triage-sg` |
| Configure storage | **30** GiB, **gp3** |
| Advanced → Credit specification | **Unlimited** (default for t3; avoids CPU throttling during training) |

Click **Launch**.

**Check:** the instance state is **Running** and status checks show **2/2 passed** (takes 1–2 minutes).

---

## 5. Attach a fixed IP (Elastic IP)

Go to **EC2 → Elastic IPs → Allocate Elastic IP address → Allocate**. Then **Actions → Associate** with `triage-prod`.

Without this, the public IP changes on every stop or start, and HTTPS (which uses a host name based on the IP) would break.

**Check:** the instance's public IPv4 address equals the Elastic IP. Note it down as `<EIP>`.

---

## 6. First login and OS updates

```bash
ssh -i ~/.ssh/triage-key.pem ubuntu@<EIP>
sudo apt update && sudo apt -y upgrade
sudo timedatectl set-timezone UTC        # keep the host in UTC; tenant time zones are handled by the scheduler
sudo reboot                              # only if the upgrade asks for it; then ssh in again
```

**Check:** `lsb_release -d` shows Ubuntu 24.04, and `timedatectl` shows UTC.

---

## 7. Add swap (safety net against out-of-memory)

```bash
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swap.conf && sudo sysctl --system
```

**Check:** `free -h` shows about 7.6 GB of memory and 4.0 GB of swap.

---

## 8. Install Docker Engine and Compose

```bash
sudo apt -y install ca-certificates curl git make jq
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt update
sudo apt -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker ubuntu           # run docker without sudo
sudo systemctl enable docker             # start on boot (containers restart themselves)
```

Log out and back in, so the group change applies.

Cap Docker's log size so logs can't fill the disk:

```bash
echo '{"log-driver":"json-file","log-opts":{"max-size":"20m","max-file":"5"}}' | sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
```

**Check:**
- `docker run --rm hello-world` prints "Hello from Docker!"
- `docker compose version` prints v2.x

---

## 9. Give the server read access to the GitHub repo

The repo is private, so use a read-only **deploy key**. Don't use your personal key.

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/github_deploy -C "triage-prod"
cat ~/.ssh/github_deploy.pub
```

On GitHub, go to **kuldeep24680/csm-triage → Settings → Deploy keys → Add deploy key**. Name it `triage-prod` and paste the key. Leave **Allow write access** off.

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
EOF
ssh-keyscan github.com >> ~/.ssh/known_hosts
git clone git@github.com:kuldeep24680/csm-triage.git ~/csm-triage
```

**Check:** `ls ~/csm-triage` shows `backend frontend deploy project_context …`.

---

## 10. Pick the HTTPS host name

There's no need to buy a domain: `sslip.io` maps a name to your IP, and Caddy gets a free Let's Encrypt certificate for it.

```bash
echo "$(curl -s https://checkip.amazonaws.com | tr . -).sslip.io"
```

Note it down as `<HOST>`, for example `13-233-10-20.sslip.io`. If you own a domain, create an **A record** pointing to `<EIP>` and use that instead.

**Check:** `getent hosts <HOST>` returns your `<EIP>`.

---

## 11. Gather the secrets for deployment

Keep these ready. They go into the server's `.env` in the deployment step:

| Secret | How |
|---|---|
| DB password | `openssl rand -hex 16` |
| JWT secret | `openssl rand -hex 32` |
| OpenAI API key | platform.openai.com → API keys (optional) |
| Slack webhook | Optional |
| Demo login password | Your choice; used by `seed-users` |

---

## Done: the server is ready

You should have:
- [ ] `t3.large`, Ubuntu 24.04, 30 GB gp3, running, 2/2 status checks
- [ ] Elastic IP attached; SSH works with `triage-key`
- [ ] Security group: 22 (your IP), 80, 443 only
- [ ] 4 GB swap, host in UTC
- [ ] Docker and Compose installed, with log rotation
- [ ] Repo cloned at `~/csm-triage` via a read-only deploy key
- [ ] `<HOST>` noted and resolving to the EIP
- [ ] Secrets ready

Next: `git pull` in `~/csm-triage`, then follow [DEPLOYMENT.md](DEPLOYMENT.md).

---

## Stop, start and tear down

| Action | Effect on cost |
|---|---|
| **Stop** the instance | Compute billing stops. You still pay for disk (~$2.4/month) and the idle Elastic IP (~$3.6/month). Docker containers come back automatically on start |
| **Start** again | Same IP (Elastic), same data |
| **Tear down** | Terminate the instance → release the Elastic IP → delete the security group and key pair |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ssh: Connection timed out` | Your IP changed. Update the SSH rule's source to **My IP** in `triage-sg` |
| `Permission denied (publickey)` | Use user `ubuntu` and `-i ~/.ssh/triage-key.pem`; the key file must be mode 400 |
| `permission denied ... docker.sock` | You didn't log out and back in after `usermod -aG docker` |
| `git clone` asks for a password or fails | Use the SSH URL (`git@github.com:...`) and check the deploy key was added to *this* repo |
