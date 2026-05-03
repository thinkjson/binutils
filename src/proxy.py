import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from pydo import Client
from tenacity import retry, retry_if_result, stop_after_attempt, wait_exponential

DEFAULT_REGION = "nyc3"
DEFAULT_SIZE = "s-1vcpu-512mb-10gb"
DEFAULT_IMAGE = "ubuntu-24-04-x64"
HOURLY_COST_USD = 0.00595
MIN_HOURS = 1 / 60  # Minimum billing time of 1 minute
SSH_LOCAL_PORT = 1080
SSH_REMOTE_PORT = 22

def find_default_public_key() -> Path:
    ssh_dir = Path.home() / ".ssh"
    candidates = [
        ssh_dir / "id_ed25519.pub",
        ssh_dir / "id_rsa.pub",
        ssh_dir / "id_ecdsa.pub",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "No default SSH public key found. Expected one of: "
        "~/.ssh/id_ed25519.pub, ~/.ssh/id_rsa.pub, ~/.ssh/id_ecdsa.pub"
    )


def get_or_create_ssh_key(client: Client, public_key: str, key_name: str) -> int:
    page = 1
    per_page = 200
    while True:
        response = client.ssh_keys.list(per_page=per_page, page=page)
        keys = response.get("ssh_keys", [])
        if not keys:
            break
        for key in keys:
            if key.get("public_key") == public_key:
                return int(key["id"])
        if len(keys) < per_page:
            break
        page += 1

    created = client.ssh_keys.create(body={"name": key_name, "public_key": public_key})
    return int(created["ssh_key"]["id"])


def create_droplet(client: Client, ssh_key_id: int) -> int:
    droplet_name = f"proxy-{int(time.time())}"
    response = client.droplets.create(
        body={
            "name": droplet_name,
            "region": DEFAULT_REGION,
            "size": DEFAULT_SIZE,
            "image": DEFAULT_IMAGE,
            "ssh_keys": [ssh_key_id],
            "ipv6": False,
            "backups": False,
            "monitoring": False,
        }
    )
    return int(response["droplet"]["id"])


def wait_for_droplet_ip(
    client: Client, droplet_id: int, timeout_seconds: int = 300
) -> str:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        details = client.droplets.get(droplet_id)
        droplet = details.get("droplet", {})
        status = droplet.get("status")
        v4_networks = droplet.get("networks", {}).get("v4", [])
        public_ip = next(
            (
                item.get("ip_address")
                for item in v4_networks
                if item.get("type") == "public"
            ),
            None,
        )
        if status == "active" and public_ip:
            return str(public_ip)
        time.sleep(3)
    raise TimeoutError(
        "Timed out waiting for droplet to become active and receive a public IP"
    )


def wait_for_tcp_port(
    host: str, port: int, timeout_seconds: int = 180, poll_interval: float = 2.0
) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return
        except OSError:
            time.sleep(poll_interval)

    raise TimeoutError(f"Timed out waiting for {host}:{port} to accept connections")


def destroy_droplet(client: Client, droplet_id: int) -> None:
    client.droplets.destroy(droplet_id)


def build_ssh_command(ip: str) -> list[str]:
    return [
        "ssh",
        "-D",
        str(SSH_LOCAL_PORT),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-C",
        "-N",
        f"root@{ip}",
    ]


def report_ssh_retry(retry_state) -> None:
    exit_code = retry_state.outcome.result()
    delay = retry_state.next_action.sleep
    print(
        f"ssh exited with status {exit_code}; retrying in {delay:.1f}s",
        file=sys.stderr,
    )


@retry(
    retry=retry_if_result(lambda exit_code: exit_code != 0),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(6),
    before_sleep=report_ssh_retry,
    retry_error_callback=lambda retry_state: retry_state.outcome.result(),
)
def run_ssh_command(ip: str) -> int:
    ssh_cmd = build_ssh_command(ip)
    print("Running:", " ".join(ssh_cmd))
    ssh_proc = subprocess.Popen(ssh_cmd)

    try:
        time.sleep(1)
        print("SSH tunnel established.")
        return ssh_proc.wait()
    except KeyboardInterrupt:
        print("\nCtrl+C received, terminating ssh tunnel...")
        if ssh_proc.poll() is None:
            ssh_proc.terminate()
            try:
                ssh_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                ssh_proc.kill()
                ssh_proc.wait(timeout=5)
        raise


def main() -> int:
    token = os.environ.get("DIGITAL_OCEAN_TOKEN")
    if not token:
        print("DIGITAL_OCEAN_TOKEN is not set", file=sys.stderr)
        return 1

    try:
        pubkey_path = find_default_public_key()
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    public_key = pubkey_path.read_text(encoding="utf-8").strip()
    key_name = f"proxy-{socket.gethostname()}"

    client = Client(token=token)

    droplet_id = None
    started_at = time.monotonic()
    exit_code = 0

    try:
        ssh_key_id = get_or_create_ssh_key(client, public_key, key_name)
        print(f"Using SSH key id: {ssh_key_id}")

        droplet_id = create_droplet(client, ssh_key_id)
        print(f"Created droplet id: {droplet_id}, waiting for droplet to obtain an IP address...")

        ip = wait_for_droplet_ip(client, droplet_id)
        print(f"Droplet ready at {ip}")
        print(f"Waiting for SSH port {SSH_REMOTE_PORT} to start listening...")
        wait_for_tcp_port(ip, SSH_REMOTE_PORT)
        print(f"SSH port is accepting connections, took {int(time.monotonic() - started_at)}s")

        try:
            exit_code = run_ssh_command(ip)
            if exit_code != 0:
                print(f"ssh exited with status {exit_code}", file=sys.stderr)
                print(
                    "Holding droplet open for manual debugging. Press Ctrl+C when done.",
                    file=sys.stderr,
                )
                while True:
                    time.sleep(1)
        except KeyboardInterrupt:
            print("\nCtrl+C received, terminating ssh tunnel...")

    except Exception as exc:
        print(f"proxy failed: {exc}", file=sys.stderr)
        exit_code = 1
    finally:
        if droplet_id is not None:
            try:
                print(f"Shutting down droplet id: {droplet_id}")
                destroy_droplet(client, droplet_id)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"Failed to shut down droplet {droplet_id}: {exc}", file=sys.stderr
                )

        elapsed_hours = (time.monotonic() - started_at) / 3600.0
        estimated_cost = max(elapsed_hours, MIN_HOURS) * HOURLY_COST_USD
        print(f"Estimated cost: ${estimated_cost:.6f} (at ${HOURLY_COST_USD:.3f}/hour)")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
